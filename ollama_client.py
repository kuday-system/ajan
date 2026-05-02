
import json
import requests
from typing import Dict, Any

from config import OLLAMA_URL, OLLAMA_TIMEOUT, OLLAMA_TEMPERATURE, DEFAULT_MODEL


class OllamaClient:
    def __init__(self, model: str = DEFAULT_MODEL, url: str = OLLAMA_URL):
        self.model = model
        self.url = url

    def call_model(self, prompt: str) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": OLLAMA_TEMPERATURE,
            }
        }

        try:
            response = requests.post(
                self.url,
                json=payload,
                timeout=OLLAMA_TIMEOUT
            )
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Ollama'ya bağlanılamadı. Ollama çalışıyor mu?")
        except requests.exceptions.Timeout:
            raise TimeoutError(f"Ollama {OLLAMA_TIMEOUT} saniyede cevap vermedi.")

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Ollama HTTP hatası: {e}")

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise ValueError("Ollama geçerli JSON API cevabı döndürmedi.") from e

        raw = data.get("response", None)
        

        if raw is None:
            raise ValueError("Ollama cevabında 'response' alanı yok.")
        if not isinstance(raw, str):
            raise ValueError("Ollama 'response' alanı string değil.")
        if not raw.strip():
            raise ValueError("Ollama boş cevap döndürdü.")

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Model geçerli JSON döndürmedi: {raw}") from e