import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:8090/v1")
os.environ.setdefault("LLM_MODEL", "stub-model")
os.environ.setdefault("VERIFIER_URL", "http://localhost:8082")
os.environ.setdefault("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
