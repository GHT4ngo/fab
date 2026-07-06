/*
  silver_cards — one row per FaB printing (set × edition × foiling)

  Cardmarket EUR matching is ANCHORED on the tcgcsv USD price (tcgplayer_product_id is
  the linking key). Cards are matched on the BARE name (pitch colour is display-only, not
  a key — Cardmarket is inconsistent about the "(Red/Yellow/Blue)" suffix). Tiers, best
  linkage first:
    1. ANCHORED — among ALL Cardmarket products sharing the bare name (any expansion),
                  pick the one whose EUR (in SEK) is closest to the tcgcsv USD anchor.
                  The anchor price + the card id/number separate same-named cards.
    2. AUTO     — no tcgcsv anchor: within the printing's expansion, foil-pair price-rank
                  + collectors_centre technique.
    3. FALLBACK — bare-name EUR aggregation across all expansions (fuzzy).
    4. (in the second SELECT) tcgcsv-sourced card for sets the-fab-cube is missing.
    5. MANUAL   — fab_cm_manual crosswalk, LAST resort only (least trusted).

  Sources:
    bronze.fab_printings         — The Fab Cube card data (names, stats, text, images, ids)
    bronze.tcgcsv_cards/_prices  — tcgcsv: USD prices + missing-set cards (the anchor)
    bronze.fab_expansions        — set_id + edition → idExpansion mapping (tier 2 only)
    bronze.cardmarket_products   — Cardmarket product catalogue
    bronze.cardmarket_prices     — EUR prices (trend per product)
    bronze.collectors_centre     — Foil types per card from fabtcg.com (tier 2 only)
    bronze.fab_cm_manual         — Manual crosswalk (tier 5, last resort)
    bronze.exchange_rates        — Riksbank EUR/SEK + USD/SEK rates
*/

with

-- ── Base card data ────────────────────────────────────────────────────────────
printings as (
    select
        printing_unique_id,
        id                                          as display_id,
        set_id,
        edition,
        coalesce(nullif(foiling, ''), 'S')           as foiling, -- S=Standard R=Rainbow C=Cold
        rarity,
        raw_data->>'name'                           as name,
        raw_data->>'pitch'                          as pitch,
        -- Name key for Cardmarket matching: the BARE card name (no pitch colour).
        -- The colour is display-only, not a key; the tcgcsv USD anchor picks the right
        -- product, and the card id/number distinguishes same-named cards.
        upper(trim(raw_data->>'name'))              as name_key,
        raw_data->>'cost'                           as cost,
        raw_data->>'power'                          as power,
        raw_data->>'defense'                        as defense,
        raw_data->>'health'                         as health,
        raw_data->>'intelligence'                   as intelligence,
        raw_data->>'type_text'                      as type_text,
        raw_data->>'functional_text'                as functional_text,
        raw_data->>'image_url'                      as image_url,
        (raw_data->>'tcgplayer_product_id')::bigint as tcgplayer_product_id
    from bronze.fab_printings
),

-- ── Exchange rates ────────────────────────────────────────────────────────────
eur_to_sek as (
    select rate_value
    from bronze.exchange_rates er
    where series_id = 'EUR/SEK'
      and rate_date = (select max(rate_date) from bronze.exchange_rates where series_id = er.series_id)
),

usd_to_sek as (
    select rate_value
    from bronze.exchange_rates er
    where series_id = 'USD/SEK'
      and rate_date = (select max(rate_date) from bronze.exchange_rates where series_id = er.series_id)
),

-- ── Latest Cardmarket prices per product ─────────────────────────────────────
latest_prices as (
    select
        idproduct,
        trend       as trend_eur,
        trend_foil  as trend_foil_eur,
        low,
        low_foil
    from bronze.cardmarket_prices
    where loaded_date = (select max(loaded_date) from bronze.cardmarket_prices)
),

-- ── tcgcsv USD price per printing — the ANCHOR KEY ───────────────────────────
-- tcgcsv mirrors TCGplayer prices per (productId, sub_type). We join our printing's
-- tcgplayer_product_id (= the tcgcsv productId, the primary key we link everything by)
-- and map our (edition, foiling) to tcgcsv's sub_type. tcgcsv uses TWO sub_type styles:
--   * edition-qualified for older sets: '1st Edition Normal', 'Unlimited Edition
--     Rainbow Foil', '1st Edition Cold Foil', …
--   * bare for newer sets: 'Normal', 'Rainbow Foil', 'Cold Foil'.
-- We match either, preferring the edition-qualified one, and take the USD market price.
-- This is BOTH the USD figure shown AND the anchor used to pick the right Cardmarket
-- product below.
tcg_prices as (
    select distinct on (p.printing_unique_id)
        p.printing_unique_id,
        tp.market_price                             as tcg_price_usd,
        tp.fetched_at                               as tcg_fetched_at
    from printings p
    join bronze.tcgcsv_prices tp
        on  tp.product_id = p.tcgplayer_product_id
        and tp.sub_type in (
            case p.foiling
                when 'S' then 'Normal' when 'R' then 'Rainbow Foil'
                when 'C' then 'Cold Foil' when 'G' then 'Gold Foil' end,
            (case p.edition when 'F' then '1st Edition '
                            when 'A' then '1st Edition '
                            when 'U' then 'Unlimited Edition ' else '' end)
            || (case p.foiling
                when 'S' then 'Normal' when 'R' then 'Rainbow Foil'
                when 'C' then 'Cold Foil' when 'G' then 'Gold Foil' end)
        )
        and tp.market_price is not null
    order by p.printing_unique_id,
        case when tp.sub_type ilike '%edition%' then 0 else 1 end
),

-- ── collectors_centre: confirmed foil technique per printing (display + heuristic) ─
cc as (
    select distinct on (p.printing_unique_id)
        p.printing_unique_id,
        cc.printing_technique                       as cc_technique
    from printings p
    join bronze.collectors_centre cc
        on cc.set_code = p.set_id
        and cc.card_id = p.display_id
        and (
            (p.foiling = 'S' and (cc.printing_technique ilike '%normal%'
                                   or cc.printing_technique ilike '%regular%'))
            or (p.foiling = 'R' and cc.printing_technique ilike '%rainbow%')
            or (p.foiling = 'C' and cc.printing_technique ilike '%cold%')
            or (p.foiling = 'G' and cc.printing_technique ilike '%gold%')
        )
    order by p.printing_unique_id, cc.printing_technique
),

-- ════════════════════════════════════════════════════════════════════════════
--  Cardmarket EUR matching — ANCHORED on the tcgcsv USD price.
--  Reliability order (best first): 1 anchored → 2 auto → 3 fallback → 5 manual.
--  The hand-built crosswalk is deliberately LAST (it had ~146 wrong foil/edition
--  picks); the tcgcsv price now disambiguates the correct Cardmarket product instead.
-- ════════════════════════════════════════════════════════════════════════════

-- Cardmarket products with the pitch-colour suffix stripped → matched on the BARE card
-- name. The colour is display-only (not a key); Cardmarket is inconsistent about it
-- (cheap normals often omit "(Red)" while only the foil carries it), so stripping it lets
-- the anchor see every product for the card. base_name is the join key.
cm_products as (
    select
        cp.idproduct,
        cp.id_expansion,
        upper(trim(regexp_replace(cp.name, '\s*\((Red|Yellow|Blue)\)\s*$', '', 'i'))) as base_name,
        nullif(pr.trend_eur, 0)                     as price_eur,
        pr.low
    from bronze.cardmarket_products cp
    left join latest_prices pr on pr.idproduct = cp.idproduct
),

-- Candidate Cardmarket products per printing within the printing's expansion (heuristic
-- path only). One name can have a foil/non-foil pair within an expansion.
cm_candidates as (
    select
        p.printing_unique_id,
        p.set_id,
        p.display_id,
        p.foiling,
        cp.idproduct,
        cp.price_eur,
        cp.low,
        tp.tcg_price_usd
    from printings p
    join bronze.fab_expansions exp
        on  exp.set_id  = p.set_id
        and exp.edition = p.edition
    join cm_products cp
        on  cp.id_expansion = exp.idexpansion
        and cp.base_name    = p.name_key
    left join tcg_prices tp on tp.printing_unique_id = p.printing_unique_id
),

-- 1) ANCHORED: with a tcgcsv USD price, pick the Cardmarket product whose EUR (in SEK)
--    is closest to it. The pool is ALL products sharing this card's BARE name across
--    EVERY expansion — the tcgcsv anchor (not name/expansion) disambiguates the correct
--    printing, and the card id/number separates same-named cards (e.g. promo alt-arts).
cm_anchor_pool as (
    select
        p.printing_unique_id,
        cp.idproduct,
        cp.price_eur,
        cp.price_eur     * (select rate_value from eur_to_sek) as eur_sek,
        tp.tcg_price_usd * (select rate_value from usd_to_sek) as usd_sek
    from printings p
    join tcg_prices tp on tp.printing_unique_id = p.printing_unique_id   -- anchored only
    join cm_products cp on cp.base_name = p.name_key
),

cm_anchored_pick as (
    select distinct on (printing_unique_id)
        printing_unique_id,
        idproduct,
        price_eur,
        eur_sek,
        usd_sek
    from cm_anchor_pool
    where price_eur is not null
    order by printing_unique_id, abs(eur_sek - usd_sek) asc, price_eur asc
),

-- Sanity guard: if even the CLOSEST candidate is wildly off the anchor, Cardmarket
-- simply has no product for this printing (e.g. HNT055 Cindra token $0.08 whose only
-- bare-name candidates are the €5.72 armory-deck hero and the €30 Marvel; or cold-foil
-- WTR080 Breaking Scales $94 where CM only lists the €0.75 regular). Matching anyway
-- attaches a same-named-but-different product. These printings get NO automated CM
-- match (manual crosswalk still allowed) and fall through to the tcgcsv USD price.
-- Bounds are deliberately extreme (≥20× AND ≥50 SEK) so the documented genuine EU/US
-- market gaps in the 3-20× band keep their Cardmarket price (basis stays CM-first).
cm_anchor_rejected as (
    select printing_unique_id
    from cm_anchored_pick
    where greatest(eur_sek, usd_sek) / nullif(least(eur_sek, usd_sek), 0) >= 20
      and abs(eur_sek - usd_sek) >= 50
),

cm_anchored as (
    select
        printing_unique_id,
        idproduct,
        price_eur,
        1                                           as match_tier,
        null::text                                  as cc_technique
    from cm_anchored_pick
    where printing_unique_id not in (select printing_unique_id from cm_anchor_rejected)
),

-- 2) AUTO (heuristic): no tcgcsv anchor → foil-pair price-rank + collectors_centre.
--    Non-foil = cheaper of a pair; foil = dearer; single product needs CC confirmation
--    (or no CC data at all for the card).
cm_heuristic_ranked as (
    select
        c.printing_unique_id,
        c.foiling,
        c.idproduct,
        c.price_eur,
        c.low,
        cc.cc_technique,
        count(*) over (partition by c.printing_unique_id)                  as products_for_card,
        row_number() over (partition by c.printing_unique_id
                           order by coalesce(c.low, 9999) asc)             as price_rank,
        count(cc.cc_technique) over (partition by c.set_id, c.display_id)  as cc_rows_for_card
    from cm_candidates c
    left join cc on cc.printing_unique_id = c.printing_unique_id
),

-- Low-volume products often have listings (low) but no computed trend. Price from
-- trend, else the product's own low (still the EXACT matched product — better than
-- the cross-expansion fallback average). A match with NEITHER is excluded so the
-- printing cascades to tier 3 instead of sitting priceless — previously ~658
-- printings were "matched" to a priceless product, which BLOCKED the fallback.
cm_heuristic as (
    select distinct on (printing_unique_id)
        printing_unique_id,
        idproduct,
        coalesce(price_eur, nullif(low, 0))         as price_eur,
        2                                           as match_tier,
        cc_technique
    from cm_heuristic_ranked
    where (price_eur is not null or nullif(low, 0) is not null)
      and ((
        products_for_card = 2
        and ((foiling = 'S'  and price_rank = 1)
          or (foiling != 'S' and price_rank = 2))
    ) or (
        products_for_card = 1
        and (cc_technique is not null or cc_rows_for_card = 0)
    ))
    order by printing_unique_id, price_rank
),

-- 3) FALLBACK: name-aggregated EUR across all expansions (fuzzy, last automated option).
cm_fallback_agg as (
    select
        base_name,
        nullif(avg(price_eur), 0)                   as trend_eur
    from cm_products
    group by base_name
),

cm_fallback as (
    select
        p.printing_unique_id,
        null::int                                   as idproduct,
        agg.trend_eur                               as price_eur,
        3                                           as match_tier,
        null::text                                  as cc_technique
    from printings p
    join cm_fallback_agg agg on agg.base_name = p.name_key
    where agg.trend_eur is not null   -- a priceless tier-3 row would only block manual
),

-- 5) MANUAL (LAST resort): the hand-built crosswalk, used only when nothing above
--    matched. Foil code → foiling resolves the correct printing_unique_id.
cm_manual as (
    select distinct on (p.printing_unique_id)
        p.printing_unique_id,
        m.idproduct,
        coalesce(nullif(pr.trend_eur, 0), nullif(pr.low, 0)) as price_eur,
        5                                           as match_tier,
        null::text                                  as cc_technique
    from bronze.fab_cm_manual m
    join bronze.fab_printings p
        on  p.id      = m.id
        and p.set_id  = m.set_id
        and p.edition = m.edition
        and (
            (m.foil in ('N')                             and p.foiling = 'S')
            or (m.foil in ('CF', 'EACF', 'AACF')         and p.foiling = 'C')
            or (m.foil in ('RF', 'EARF', 'AARF', 'EXRF') and p.foiling = 'R')
            or (m.foil in ('GF')                         and p.foiling = 'G')
        )
    join latest_prices pr on pr.idproduct = m.idproduct
    where m.idproduct is not null
    order by p.printing_unique_id, m.idproduct
),

-- Best Cardmarket match per printing: lowest match_tier wins (anchored beats manual).
-- Anchor-rejected printings skip the heuristic/fallback tiers too — those tiers draw
-- from the same (wrong) bare-name pool the anchor already ruled out. Manual stays: a
-- hand-curated crosswalk row is an explicit human override.
best_match_all as (
    select printing_unique_id, idproduct, price_eur, match_tier, cc_technique from cm_anchored
    union all
    select printing_unique_id, idproduct, price_eur, match_tier, cc_technique from cm_heuristic
    where printing_unique_id not in (select printing_unique_id from cm_anchor_rejected)
    union all
    select printing_unique_id, idproduct, price_eur, match_tier, cc_technique from cm_fallback
    where printing_unique_id not in (select printing_unique_id from cm_anchor_rejected)
    union all
    select printing_unique_id, idproduct, price_eur, match_tier, cc_technique from cm_manual
),

best_match as (
    select distinct on (printing_unique_id)
        printing_unique_id, idproduct, price_eur, match_tier, cc_technique
    from best_match_all
    order by printing_unique_id, match_tier asc
),

-- (tcg_prices is defined earlier — it's the anchor for Cardmarket matching above.)

-- ── tcgcsv-sourced printings (sets the-fab-cube is missing) ───────────────────
-- Cards whose tcgplayer product_id is NOT in bronze.fab_printings — i.e. cards the
-- the-fab-cube catalogue lacks (PEN, OMN, promos, decks, future gaps). Each card is
-- expanded to one printing per priced sub_type (foiling); cards with no price still
-- get one Normal row. These are unioned in below as match_tier = 4.
-- tcgcsv names sometimes embed the collector number (e.g. "Adaptive Plating - FAB169")
-- and/or the pitch colour ("Ironsong Response (Blue)"). Strip both so the SAME card shares
-- ONE name (grouped in the UI); set/number live in set_id/display_id, shown when expanded.
-- name_nonum removes a trailing " - <SETCODE><digits>" first (so the colour suffix, if any,
-- is then at the end for the pitch parse below). Pitch isn't a tcgcsv field — parsed here.
-- name_np = tcgcsv name with (1) the trailing collector number and (2) the pitch colour
-- (wherever it sits) removed. Whatever trailing "(…)" remains is an alt-art treatment
-- (Marvel / Golden / Extended Art / Alternate Art …) → pulled out as `variant` so the card
-- shares its base name with all other printings and the treatment shows when expanded.
tcgcsv_src as (
    select
        c.*,
        regexp_replace(
            regexp_replace(c.name, '\s*[-(]\s*[A-Z]{2,}[0-9]+\)?\s*$', ''),  -- collector number
            '\s*\((Red|Yellow|Blue)\)', '', 'gi'                            -- pitch colour
        )                                                              as name_np,
        case
            when c.name ~* '\(red\)'    then '1'
            when c.name ~* '\(yellow\)' then '2'
            when c.name ~* '\(blue\)'   then '3'
        end                                                            as derived_pitch
    from bronze.tcgcsv_cards c
    -- TCGplayer/tcgcsv includes composite products with collector numbers like
    -- "ARC001 // ARC003" or "NUU028/NUU029" (hero + weapon, token pair, etc.).
    -- Those are sellable product pairings, not standalone card printings, and they
    -- create bogus edition-N rows for existing sets in the admin panel. Keep real
    -- split/paired card names when the collector number is a normal single id.
    where c.number not like '%/%'
      and c.number not like '%//%'
      and
      not exists (
        select 1 from bronze.fab_printings p
        where (p.raw_data->>'tcgplayer_product_id')::bigint = c.product_id
    )
),

tcgcsv_missing as (
    select
        product_id,
        set_code,
        number,
        -- a card can stack treatments, e.g. "(Left) (Golden)" or "(Marvel) (Japanese Alt)";
        -- strip ALL trailing parentheticals from the name and fold them into one variant.
        regexp_replace(name_np, '(\s*\([^()]+\))+\s*$', '')          as name,
        nullif(btrim(
            regexp_replace(substring(name_np from '((\s*\([^()]+\))+)\s*$'),
                           '\)\s*\(', ', ', 'g'),
            ' ()'), '')                                              as variant,
        derived_pitch                                                as pitch,
        rarity,
        cost, power, defense, life, intellect,
        card_type, class, talent,
        regexp_replace(coalesce(description, ''), '<[^>]+>', '', 'g') as functional_text
    from tcgcsv_src
),

tcgcsv_missing_printings as (
    select
        m.*,
        coalesce(pr.sub_type, 'Normal')             as sub_type,
        -- market_price only exists where TCGplayer saw actual sales; low-volume
        -- products (blitz-deck singles like 1HB/1HD History Pack decks) often have
        -- only listings. Fall back to low_price — the cheapest real ask — rather
        -- than leaving ~400 printings priceless.
        coalesce(pr.market_price, pr.low_price)     as price_usd,
        pr.fetched_at                               as tcg_fetched_at
    from tcgcsv_missing m
    left join bronze.tcgcsv_prices pr on pr.product_id = m.product_id
),

-- Cardmarket EUR for tcgcsv-sourced printings (added 2026-07-06): same anchored
-- pick as tier 1 — all CM products sharing the bare name, closest EUR (in SEK) to
-- this printing's USD anchor wins — including the same sanity guard (≥20× AND
-- ≥50 SEK off → CM has no product for it; keep USD only). Anchor-less printings
-- get no EUR: without a price to disambiguate same-named products across
-- expansions, a bare-name guess is worse than no price.
tcgcsv_cm_pool as (
    select
        'tcgcsv:' || mp.product_id || ':' || mp.sub_type       as printing_unique_id,
        cp.idproduct,
        cp.price_eur,
        cp.price_eur * (select rate_value from eur_to_sek)     as eur_sek,
        mp.price_usd * (select rate_value from usd_to_sek)     as usd_sek
    from tcgcsv_missing_printings mp
    join cm_products cp on cp.base_name = upper(trim(mp.name))
    where mp.price_usd is not null
      and cp.price_eur is not null
),

tcgcsv_cm_pick as (
    select distinct on (printing_unique_id)
        printing_unique_id, idproduct, price_eur, eur_sek, usd_sek
    from tcgcsv_cm_pool
    order by printing_unique_id, abs(eur_sek - usd_sek) asc, price_eur asc
),

tcgcsv_cm as (
    select printing_unique_id, idproduct, price_eur
    from tcgcsv_cm_pick
    where not (greatest(eur_sek, usd_sek) / nullif(least(eur_sek, usd_sek), 0) >= 20
               and abs(eur_sek - usd_sek) >= 50)
)

-- ── Final select ──────────────────────────────────────────────────────────────
select
    p.printing_unique_id,
    p.display_id,
    p.set_id,
    p.edition,
    p.foiling,
    p.rarity,
    p.name,
    null::text                                      as variant,   -- the-fab-cube names are clean
    p.pitch,
    p.cost,
    p.power,
    p.defense,
    p.health,
    p.intelligence,
    p.type_text,
    p.functional_text,
    p.image_url,
    p.tcgplayer_product_id,
    bm.idproduct                                    as cm_idproduct,
    bm.match_tier,
    bm.price_eur,                                   -- Cardmarket EUR only (null if no CM match)
    tcg_prices.tcg_price_usd,                       -- tcgcsv USD only (null if not priced)
    tcg_prices.tcg_fetched_at,
    -- SEK: Cardmarket EUR is the primary basis (EU market); tcgcsv USD fills the gap.
    coalesce(
        round(bm.price_eur              * e.rate_value, 0),
        round(tcg_prices.tcg_price_usd  * u.rate_value, 0)
    )                                               as price_sek,
    nullif(lp.low, 0)                               as cm_low_eur,
    -- Trade valuation (Phase 4 rule): trend price, or LOW if it's higher —
    -- greatest(trend, low) in EUR → SEK; tcgcsv USD fills when no CM match.
    -- GREATEST ignores nulls, so a missing low simply leaves trend.
    coalesce(
        round(greatest(bm.price_eur, nullif(lp.low, 0)) * e.rate_value, 0),
        round(tcg_prices.tcg_price_usd * u.rate_value, 0)
    )                                               as trade_value_sek,
    e.rate_value                                    as eur_to_sek_rate,
    coalesce(bm.cc_technique, cc.cc_technique)      as cc_technique,
    -- Which source produced price_sek, and how confident we are it's linked correctly.
    -- EUR wins when present; its confidence reflects the CM match method (manual/auto/
    -- fallback). When EUR is absent the tcgcsv USD price (exact productId link) is used.
    case
        when bm.price_eur is not null then
            case bm.match_tier
                when 1 then 'cardmarket_anchored'   -- picked via tcgcsv USD anchor
                when 2 then 'cardmarket_auto'       -- foil-pair heuristic
                when 3 then 'cardmarket_fallback'   -- name aggregation
                when 5 then 'cardmarket_manual'     -- hand crosswalk (last resort)
                else        'cardmarket'
            end
        when tcg_prices.tcg_price_usd is not null then 'tcgcsv_usd'
    end                                             as price_source,
    case
        when bm.price_eur is not null then
            case bm.match_tier
                when 1 then 'high'      -- tcgcsv-validated Cardmarket pick
                when 2 then 'medium'    -- name+expansion+foil heuristic
                when 3 then 'low'       -- name aggregation
                when 5 then 'low'       -- manual crosswalk, now least-trusted
                else        'medium'
            end
        when tcg_prices.tcg_price_usd is not null then 'high'   -- exact productId link
    end                                             as price_confidence

from printings p
cross join eur_to_sek e
cross join usd_to_sek u
left join best_match bm on bm.printing_unique_id = p.printing_unique_id
left join latest_prices lp on lp.idproduct = bm.idproduct
left join cc           on cc.printing_unique_id  = p.printing_unique_id
left join tcg_prices   on tcg_prices.printing_unique_id = p.printing_unique_id

union all

-- ── tcgcsv-sourced cards (match_tier = 4) ────────────────────────────────────
-- Cards the-fab-cube is missing, sourced from tcgcsv. foiling comes from the price
-- sub_type; edition is unknown to tcgcsv so it's 'N'. image_url is the high-res CDN
-- image built from the product id. USD (market price) drives price_sek; no EUR.
select
    'tcgcsv:' || mp.product_id || ':' || mp.sub_type           as printing_unique_id,
    mp.number                                                  as display_id,
    mp.set_code                                                as set_id,
    case
        when mp.sub_type ilike '1st edition%' then 'F'
        when mp.sub_type ilike 'unlimited%'   then 'U'
        else 'N'
    end                                                        as edition,
    case
        when mp.sub_type ilike '%cold foil%'    then 'C'
        when mp.sub_type ilike '%rainbow foil%' then 'R'
        when mp.sub_type ilike '%gold foil%'    then 'G'
        else 'S'
    end                                                        as foiling,
    mp.rarity,
    mp.name,
    mp.variant,
    mp.pitch,
    mp.cost,
    mp.power,
    mp.defense,
    mp.life                                                    as health,
    mp.intellect                                              as intelligence,
    nullif(trim(concat_ws(' ', mp.talent, mp.class, mp.card_type)), '') as type_text,
    nullif(mp.functional_text, '')                            as functional_text,
    'https://tcgplayer-cdn.tcgplayer.com/product/'
        || mp.product_id || '_in_1000x1000.jpg'                as image_url,
    mp.product_id                                             as tcgplayer_product_id,
    tc.idproduct                                              as cm_idproduct,
    4                                                          as match_tier,
    tc.price_eur                                              as price_eur,
    mp.price_usd                                              as tcg_price_usd,
    mp.tcg_fetched_at                                         as tcg_fetched_at,
    -- Same CM-first basis as the main path: EUR when anchored-matched, else USD.
    coalesce(
        round(tc.price_eur * e.rate_value, 0),
        round(mp.price_usd * u.rate_value, 0)
    )                                                          as price_sek,
    nullif(lp4.low, 0)                                        as cm_low_eur,
    coalesce(
        round(greatest(tc.price_eur, nullif(lp4.low, 0)) * e.rate_value, 0),
        round(mp.price_usd * u.rate_value, 0)
    )                                                          as trade_value_sek,
    e.rate_value                                             as eur_to_sek_rate,
    null::text                                                as cc_technique,
    case
        when tc.price_eur is not null then 'cardmarket_anchored'
        when mp.price_usd is not null then 'tcgcsv_usd'
    end                                                        as price_source,
    case
        when tc.price_eur is not null or mp.price_usd is not null then 'high'
    end                                                        as price_confidence
from tcgcsv_missing_printings mp
cross join eur_to_sek e
cross join usd_to_sek u
left join tcgcsv_cm tc
    on tc.printing_unique_id = 'tcgcsv:' || mp.product_id || ':' || mp.sub_type
left join latest_prices lp4 on lp4.idproduct = tc.idproduct
