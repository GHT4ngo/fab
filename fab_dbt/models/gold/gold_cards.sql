/*
  gold_cards — API-ready FaB card catalogue
  One row per printing (set × edition × foiling).
  match_tier shows how the price was matched:
    1 = anchored  (Cardmarket product picked via the tcgcsv USD price)
    2 = auto      (expansion + name + foil heuristic, no tcgcsv anchor)
    3 = fallback  (name aggregation across expansions)
    4 = tcgcsv-sourced card (set missing from the-fab-cube)
    5 = manual    (hand crosswalk — LAST resort, least trusted)
    null = no price found
  USD prices (tcg_price_usd) come from tcgcsv.com; price_source/price_confidence
  expose which source produced price_sek and how reliable the linkage is.
*/

select
    printing_unique_id,
    display_id,
    set_id,
    edition,
    foiling,
    rarity,
    name,
    variant,                                            -- alt-art treatment (Marvel/Golden/…), null if none
    pitch,
    cost,
    power,
    defense,
    health,
    intelligence,
    type_text,
    functional_text,
    image_url,
    tcgplayer_product_id,
    cm_idproduct,
    match_tier,
    price_eur,                                          -- Cardmarket EUR (null if unmatched)
    tcg_price_usd,                                      -- JustTCG USD (null if not fetched)
    tcg_fetched_at,
    price_sek,
    eur_to_sek_rate,
    cc_technique,
    price_source,                                          -- which source produced price_sek
    price_confidence,                                      -- high / medium / low

    foiling != 'S'                                          as is_foil,
    (price_eur is not null or tcg_price_usd is not null)    as has_price

from {{ ref('silver_cards') }}
