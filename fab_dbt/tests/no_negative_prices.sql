select *
from {{ ref('gold_cards') }}
where coalesce(price_eur, 0) < 0
   or coalesce(tcg_price_usd, 0) < 0
   or coalesce(price_sek, 0) < 0
