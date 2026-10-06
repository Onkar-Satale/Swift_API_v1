"""
History-Grounded RAG Service with Persistent ChromaDB.
Indexes and retrieves verified resolution episodes (Failure -> Diagnosis -> Fix -> Success).
"""

import os
import re
import json
import hashlib
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.config.settings import logger

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROMA_PERSIST_DIR = os.path.join(BASE_DIR, "chroma_db")
os.makedirs(CHROMA_PERSIST_DIR, exist_ok=True)
EMBEDDING_DIM = 64

def _hash_token(token: str) -> int:
    return int(hashlib.md5(token.encode()).hexdigest(), 16) % EMBEDDING_DIM

def generate_dense_embedding(
    method: str,
    url: str,
    status: Any,
    error_text: str = "",
    headers_keys: Optional[List[str]] = None
) -> List[float]:
    """Generates a 64-dim normalized dense vector from request and error features."""
    vec = [0.0] * EMBEDDING_DIM
    vec[_hash_token(f"m:{str(method).upper()}")] += 2.0
    status_str = str(status)
    vec[_hash_token(f"s:{status_str}")] += 2.5
    vec[_hash_token(f"sf:{status_str[:1]}xx")] += 1.5

    clean_url = url.split("?")[0].replace("https://", "").replace("http://", "")
    for token in re.findall(r"[A-Za-z0-9_\-]+", clean_url.lower()):
        if len(token) > 1:
            vec[_hash_token(f"u:{token}")] += 1.2

    if error_text:
        for token in re.findall(r"[A-Za-z0-9_\-]+", error_text[:200].lower())[:25]:
            vec[_hash_token(f"e:{token}")] += 1.0

    if headers_keys:
        for h in headers_keys:
            vec[_hash_token(f"h:{h.lower()}")] += 0.8

    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm > 0 else vec


class ChromaRAGMemoryStore:
    """ChromaDB Persistent Vector Store for API Resolution Episodes."""

    def __init__(self):
        self.collection = None
        self._init_chroma()

    def _init_chroma(self):
        try:
            import chromadb
            client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
            self.collection = client.get_or_create_collection(
                name="api_resolution_episodes",
                metadata={"hnsw:space": "cosine"}
            )
            logger.info(f"⚡ ChromaDB ready. Total episodes: {self.collection.count()}")
            if self.collection.count() == 0:
                self._seed_default_episodes()
        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}", exc_info=True)

    def _seed_default_episodes(self):
        seeds_path = os.path.join(BASE_DIR, "rag_episodes.json")
        if os.path.exists(seeds_path):
            try:
                with open(seeds_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for uid, eps in data.items():
                        for ep in eps:
                            self.index_resolution_episode(
                                user_id=uid,
                                method=ep.get("method", "GET"),
                                url=ep.get("url", ""),
                                failed_status=ep.get("failedStatus", 404),
                                error_snippet=ep.get("errorSnippet", ""),
                                root_cause_layer=ep.get("rootCauseLayer", "General"),
                                applied_fix=ep.get("appliedFix", {}),
                                success_status=ep.get("successStatus", 200),
                                success_duration=ep.get("successDuration", 200),
                                custom_id=ep.get("episodeId")
                            )
                return
            except Exception as e:
                logger.warning(f"Could not load seeds from {seeds_path}: {e}")

    def index_resolution_episode(
        self,
        user_id: str,
        method: str,
        url: str,
        failed_status: Any,
        error_snippet: str,
        root_cause_layer: str,
        applied_fix: Dict[str, Any],
        success_status: Any = 200,
        success_duration: int = 0,
        custom_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Indexes a verified resolution episode in ChromaDB."""
        ep_id = custom_id or f"ep_{int(datetime.now().timestamp() * 1000)}"
        uid = str(user_id or "guest")
        embedding = generate_dense_embedding(
            method=method,
            url=url,
            status=failed_status,
            error_text=f"{error_snippet} {root_cause_layer}",
            headers_keys=list(applied_fix.keys()) if isinstance(applied_fix, dict) else []
        )

        metadata = {
            "episodeId": ep_id,
            "userId": uid,
            "timestamp": datetime.now().isoformat(),
            "method": method.upper(),
            "url": url,
            "failedStatus": str(failed_status),
            "errorSnippet": str(error_snippet)[:200],
            "rootCauseLayer": root_cause_layer,
            "appliedFixJson": json.dumps(applied_fix),
            "successStatus": str(success_status),
            "successDuration": int(success_duration)
        }

        if self.collection:
            self.collection.upsert(
                ids=[ep_id],
                embeddings=[embedding],
                documents=[f"{method.upper()} {url} -> {failed_status} ({root_cause_layer})"],
                metadatas=[metadata]
            )

        return {**metadata, "appliedFix": applied_fix}

    def retrieve_relevant_episodes(
        self,
        user_id: str,
        method: str,
        url: str,
        status: Any,
        error_text: str = "",
        headers_keys: Optional[List[str]] = None,
        top_k: int = 2
    ) -> List[Dict[str, Any]]:
        """Retrieves top-k similar resolution episodes via vector cosine similarity."""
        if not self.collection or self.collection.count() == 0:
            return []

        embedding = generate_dense_embedding(method, url, status, error_text, headers_keys)
        results = []

        try:
            query_res = self.collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k * 2, self.collection.count()),
                include=["metadatas", "distances"]
            )

            if query_res and query_res.get("metadatas") and query_res["metadatas"][0]:
                for meta, dist in zip(query_res["metadatas"][0], query_res["distances"][0]):
                    sim = max(0.0, round((1.0 - dist) * 100, 1))
                    if sim < 40.0:
                        continue
                    try:
                        fix_obj = json.loads(meta.get("appliedFixJson", "{}"))
                    except Exception:
                        fix_obj = {}

                    results.append({
                        "episodeId": meta.get("episodeId"),
                        "endpoint": f"{meta.get('method')} {meta.get('url')}",
                        "failedStatus": meta.get("failedStatus"),
                        "previousError": meta.get("errorSnippet"),
                        "rootCauseLayer": meta.get("rootCauseLayer"),
                        "successfulFixUsed": fix_obj,
                        "resultStatus": meta.get("successStatus"),
                        "resultDuration": meta.get("successDuration"),
                        "timestamp": meta.get("timestamp"),
                        "matchPercentage": sim
                    })
                    if len(results) >= top_k:
                        break
        except Exception as e:
            logger.error(f"Error querying ChromaDB: {e}")

        return results

    def delete_episode(self, user_id: str, episode_id: str) -> bool:
        if self.collection:
            try:
                self.collection.delete(ids=[episode_id])
                return True
            except Exception as e:
                logger.warning(f"Error deleting episode {episode_id}: {e}")
        return False

    def clear_user_episodes(self, user_id: str) -> bool:
        if self.collection:
            try:
                self.collection.delete(where={"userId": str(user_id)})
                return True
            except Exception as e:
                logger.warning(f"Error clearing episodes for {user_id}: {e}")
        return False


rag_memory_store = ChromaRAGMemoryStore()
