select *
from {{ ref('gold_cards') }}
where set_id like '%/%'
   or set_id like '%-%'
