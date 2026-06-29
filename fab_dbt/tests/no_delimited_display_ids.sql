select *
from {{ ref('gold_cards') }}
where display_id like '%/%'
