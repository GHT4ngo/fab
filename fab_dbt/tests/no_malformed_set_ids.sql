select *
from {{ ref('gold_cards') }}
where set_id like '%//%'
