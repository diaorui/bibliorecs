LIBRARIES = {
    "sclibrary": {
        "name": "Santa Clara City Library",
        "catalog_base": "https://sclibrary.bibliocommons.com",
        "gateway_base": "https://gateway.bibliocommons.com/v2/libraries/sclibrary",
        "syndetics_client": "sepup",
    },
    "sccl": {
        "name": "Santa Clara County Library",
        "catalog_base": "https://sccl.bibliocommons.com",
        "gateway_base": "https://gateway.bibliocommons.com/v2/libraries/sccl",
        "syndetics_client": "santaclaracfl",
    },
    "sjpl": {
        "name": "San Jose Public Library",
        "catalog_base": "https://sjpl.bibliocommons.com",
        "gateway_base": "https://gateway.bibliocommons.com/v2/libraries/sjpl",
        "syndetics_client": "sanjosepl",
    },
    "sunnyvale": {
        "name": "Sunnyvale Public Library",
        "catalog_base": "https://sunnyvale.bibliocommons.com",
        "gateway_base": "https://gateway.bibliocommons.com/v2/libraries/sunnyvale",
        "syndetics_client": "sunnyvaleca",
    },
    "paloalto": {
        "name": "Palo Alto City Library",
        "catalog_base": "https://paloalto.bibliocommons.com",
        "gateway_base": "https://gateway.bibliocommons.com/v2/libraries/paloalto",
        "syndetics_client": "paloaltocity",
    },
}

HALF_LIFE_DAYS = 90
MAX_SEARCH_QUERIES = 10
POOL_LIMIT = 100
TOP_CANDIDATES = 15
MMR_LAMBDA = 0.5
MMR_TOP_K = 100
RECS_PER_CAROUSEL = 3
