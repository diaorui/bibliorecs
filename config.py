import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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

EMBEDDING_MODEL = "sentence-transformers/static-retrieval-mrl-en-v1"
EMBEDDING_PATH = os.path.join(_SCRIPT_DIR, "embeddings.npy")
EMBEDDING_IDS_PATH = os.path.join(_SCRIPT_DIR, "embedding_ids.json")

TIME_DECAY_HALF_LIFE_DAYS = 90
TOP_CANDIDATES = 15
MMR_LAMBDA = 0.5
MMR_TOP_K = 100
FILTER_ENGLISH = True
NEW_BOOK_MAX_AGE_YEARS = 1

UPDATE_WINDOW_START = 2
UPDATE_WINDOW_END = 4
AUTO_RENEW_DAYS_BEFORE_DUE = 3
DEACTIVATE_MAX_RATIO = 0.2
