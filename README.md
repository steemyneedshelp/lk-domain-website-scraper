## LK Insight

A local, fully offline business intelligence tool for Sri Lankan websites. You give it a `.lk` domain, it scrapes the site, extracts key business info using a local LLM, stores it in a knowledge graph, and lets you query it through a chat interface. No external APIs, no cloud, everything runs on your machine.

---

### Tech Stack

- **Language:** Python
- **Scraping:** BeautifulSoup + Selenium (fallback for JS-heavy sites)
- **LLM:** Ollama running Gemma3:1B locally
- **Knowledge Graph:** Neo4j
- **Vector DB:** ChromaDB
- **Memory Layer:** Mem0 (local, Ollama-backed)
- **Backend:** FastAPI
- **Frontend:** Plain HTML/JS

---

### Setup

1. Install [Ollama](https://ollama.com) and pull the model:
```bash
   ollama pull gemma3:1b
   ollama pull nomic-embed-text
```

2. Install [Neo4j Desktop](https://neo4j.com/download), create an instance and a database called `lkinsight`

3. Clone the repo, activate your venv, and install dependencies:
```bash
   pip install -r requirements.txt
```

4. Start the backend:
```bash
   uvicorn main:app --reload
```

5. Open `index.html` in your browser

---

### Usage

- Paste a `.lk` URL in the scrape bar and hit Scrape
- Once scraped, ask anything about the company in the chat
- Supports multiple companies, just keep scraping

---

### What's Next

- Benchmarking alternative local models and exploring optional API-based LLM support
- Improved UI/UX
- Address extraction improvements
- Products/services noise filtering
- Unit, integration, and validation testing
- Standalone + embeddable deployment support
