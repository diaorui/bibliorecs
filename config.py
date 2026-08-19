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
POOL_LIMIT = 100
TOP_CANDIDATES = 300
MMR_LAMBDA = 0.5
MIN_COSINE = 0.75
MIN_PROFILE_AGE_HALF_LIVES = 2
MIN_SCORE = MIN_COSINE * 2 ** (-MIN_PROFILE_AGE_HALF_LIVES)
RECS_PER_CAROUSEL = 1

REFRESH_HOURS = 4
FORMATS_REFRESH_HOURS = 24

HOLDS_TTL_MIN = 15
CHECKOUTS_TTL_MIN = 60
HISTORY_TTL_MIN = 60
SYNC_MAX_CONCURRENCY = 3
SYNC_RETRY_MIN = 5
