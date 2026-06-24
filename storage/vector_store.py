
import json
import os
import time

import chromadb
import requests
from dotenv import load_dotenv

load_dotenv()

COLLECTION_NAME = "fraud_documents"
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))

GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_CHAT_MODEL = "openai/gpt-oss-20b"

OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_EMBEDDING_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2:free"


def get_groq_api_key():
    api_key = os.getenv("GROQ_API_KEY")
    return api_key


def get_openrouter_api_key():
    api_key = os.getenv("OPENROUTER_API_KEY")
    return api_key

# incase it hits the limit
def post_with_retry(url, headers, body, provider_name, max_retries=4):
    
    wait_seconds = 5
    for attempt in range(max_retries):
        response = requests.post(url, headers=headers, json=body, timeout=90)

        if response.status_code == 429:
            if attempt == max_retries - 1:
                raise RuntimeError(
                    f"{provider_name} rate limit hit too many times in a row. "
                )
            print(f"rate limited by {provider_name}  {wait_seconds}s before retrying")
            time.sleep(wait_seconds)
            wait_seconds *= 2  # back off more each time
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("kaj korena")

# sending prompts to groq
def ask_llm_for_json(system_prompt, user_prompt):
    
    headers = {
        "Authorization": f"Bearer {get_groq_api_key()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": GROQ_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    response = post_with_retry(f"{GROQ_URL}/chat/completions", headers, body, "Groq")
    reply_text = response.json()["choices"][0]["message"]["content"]

    try:
        return json.loads(reply_text)
    except json.JSONDecodeError:
        raise ValueError(f"LLM didn't return valid JSON:\n{reply_text}")

# embedding
def get_embedding(text):
    
    headers = {
        "Authorization": f"Bearer {get_openrouter_api_key()}",
        "Content-Type": "application/json",
    }
    body = {"model": OPENROUTER_EMBEDDING_MODEL, "input": [text]}

    response = post_with_retry(f"{OPENROUTER_URL}/embeddings", headers, body, "OpenRouter")
    return response.json()["data"][0]["embedding"]

# creates chroma db
def get_collection():
    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return client.get_or_create_collection(name=COLLECTION_NAME)


def save_document(doc_id, text, metadata):
    
    embedding = get_embedding(text)
    collection = get_collection()
    collection.upsert(
        ids=[doc_id],
        documents=[text],
        embeddings=[embedding],
        metadatas=[metadata],
    )

# lists everything that got saved
def list_documents(doc_type=None):
   
    collection = get_collection()

    where_filter = {"doc_type": doc_type} if doc_type else None
    result = collection.get(where=where_filter)

    documents = []
    for i in range(len(result["ids"])):
        documents.append({
            "id": result["ids"][i],
            "text": result["documents"][i],
            "metadata": result["metadatas"][i],
        })
    return documents

# to delete just one
def delete_document(doc_id):
    
    collection = get_collection()
    collection.delete(ids=[doc_id])

# to clear everything
def delete_all_documents():
    
    collection = get_collection()
    all_ids = collection.get()["ids"]
    if all_ids:
        collection.delete(ids=all_ids)
    return len(all_ids)
