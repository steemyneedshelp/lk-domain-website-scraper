from fastapi import FastAPI
from pydantic import BaseModel
import ollama
import chromadb
from neo4j import GraphDatabase
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import json
from mem0 import Memory

def extract_contacts_regex(text):
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    phones = re.findall(r'\+94[\s\-]*\(0\)[\s\-]*\d{1,2}[\s\-]*\d{1,3}[\s\-]*\d{3}[\s\-]*\d{3}|\b0\d{9}\b', text)
    hotlines = re.findall(r'\b(?:1\d{3})\b', text)
    return {
        "emails": list(set(emails)),
        "phone_numbers": list(set(phones)),
        "hotlines": list(set(hotlines))
    }

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# neo4j
NEO4J_URI = "neo4j://127.0.0.1:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "onetwothree"
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# chromadb
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(name="lkinsight")

# mem0

config = {
    "embedder": {
        "provider": "ollama",
        "config": {
            "model": "nomic-embed-text",
        }
    },
    "llm": {
        "provider": "ollama",
        "config": {
            "model": "gemma3:1b",
        }
    },
    "vector_store": {
        "provider": "chroma",
        "config": {
            "collection_name": "mem0_memories",
            "path": "./mem0_db"
        }
    }
}

memory = Memory.from_config(config)

# models
class URLInput(BaseModel):
    url: str

class QueryInput(BaseModel):
    query: str

# scraper
def scrape_with_selenium(url):
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver_chrome = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver_chrome.set_page_load_timeout(45)  # give up after 20 seconds
    
    try:
        driver_chrome.get(url)
        try:
            driver_chrome.execute_script("""
                var elements = document.querySelectorAll('[id*="cookie"], [class*="cookie"], [id*="consent"], [class*="consent"]');
                elements.forEach(function(el) { el.remove(); });
            """)
        except:
            pass
        time.sleep(5)
        driver_chrome.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        soup = BeautifulSoup(driver_chrome.page_source, "html.parser")
    except Exception as e:
        driver_chrome.quit()
        raise Exception(f"Page load timed out or failed: {str(e)}")
    finally:
        driver_chrome.quit()

    for tag in soup(["script", "style"]):
        tag.decompose()

    return soup.get_text(separator="\n", strip=True)

# llm extraction
def extract_business_info(text):
    combined = text[:3000] + "\n...\n" + text[-3000:]
    
    prompt = f"""Extract business information from the text below and return ONLY a valid JSON object.
Strict rules:
- No comments
- No trailing commas
- Use null for missing fields
- All strings must be quoted
- products_services must be a flat list of strings only containing actual business products or services, not navigation items, news, careers, or blog links
- website_description must be one simple sentence about what the company does, ignore any cookie or legal text

Fields to extract:
- company_name
- address
- products_services
- website_description

Text:
{combined}

Return only the JSON object, nothing else.
"""
    response = ollama.chat(
        model="gemma3:1b",
        messages=[{"role": "user", "content": prompt}]
    )
    return response["message"]["content"]

# json parser
def parse_llm_output(raw):
    raw = re.sub(r'//.*', '', raw)
    raw = re.sub(r'\\(?!["\\/bfnrt])', r'\\\\', raw)
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError as e:
            print("json parse error:", e)
            print("raw output:", raw)
            return None
    return None

# neo4j store
def store_in_neo4j(data, url):
    with driver.session(database="lkinsight") as session:
        # clear old data for this url first
        session.run("""
            MATCH (c:Company {url: $url})
            OPTIONAL MATCH (c)-[:HAS_PHONE]->(p)
            OPTIONAL MATCH (c)-[:HAS_EMAIL]->(e)
            DETACH DELETE p, e
        """, url=url)
        
        session.run("""
            MERGE (c:Company {url: $url})
            SET c.name = $name,
                c.address = $address,
                c.description = $description
        """, name=data["company_name"], url=url,
             address=data["address"],
             description=data["website_description"])

        for phone in data.get("phone_numbers", []):
            if phone:
                session.run("""
                    MATCH (c:Company {url: $url})
                    MERGE (p:Phone {number: $phone})
                    MERGE (c)-[:HAS_PHONE]->(p)
                """, url=url, phone=phone)

        for email in data.get("emails", []):
            if email:
                session.run("""
                    MATCH (c:Company {url: $url})
                    MERGE (e:Email {address: $email})
                    MERGE (c)-[:HAS_EMAIL]->(e)
                """, url=url, email=email)

# chroma store
def store_in_chroma(data, url):
    try:
        collection.delete(ids=[url])
    except:
        pass
    
    document = f"""
    Company: {data['company_name']}
    Address: {data['address']}
    Products/Services: {', '.join(data['products_services']) if isinstance(data['products_services'], list) else str(data['products_services'])}
    Description: {data['website_description']}
    """
    
    collection.add(
        documents=[document],
        ids=[url],
        metadatas=[{"company_name": data['company_name'], "url": url}]
    )

# endpoints
@app.get("/")
def root():
    return {"message": "LK Insight API is running"}

@app.post("/scrape")
def scrape(input: URLInput):
    try:
        text = scrape_with_selenium(input.url)
        print("LAST 3000 CHARS:", text[-3000:])
        raw = extract_business_info(text)
        print("RAW LLM OUTPUT:", raw)
        parsed = parse_llm_output(raw)
        parsed = parse_llm_output(raw)
        if parsed is None:
            return {"status": "error", "message": "LLM failed to return valid JSON"}

        # ensure all fields exist
        parsed.setdefault("company_name", None)
        parsed.setdefault("address", None)
        parsed.setdefault("products_services", [])
        parsed.setdefault("website_description", None)
        parsed.setdefault("emails", [])
        parsed.setdefault("phone_numbers", [])
        contacts = extract_contacts_regex(text)
        parsed["emails"] = contacts["emails"]
        parsed["phone_numbers"] = contacts["phone_numbers"] + contacts["hotlines"]
        store_in_neo4j(parsed, input.url)
        store_in_chroma(parsed, input.url)
        return {"status": "success", "data": parsed}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/query")
def query(input: QueryInput):
    results = collection.query(
        query_texts=[input.query],
        n_results=3
    )
    return {"results": results}

class ChatInput(BaseModel):
    user_id: str
    message: str

@app.post("/chat")
def chat(input: ChatInput):
    # find relevant company from chromadb
    chroma_results = collection.query(query_texts=[input.message], n_results=1)
    company_name = None
    if chroma_results["metadatas"] and chroma_results["metadatas"][0]:
        company_name = chroma_results["metadatas"][0][0].get("company_name")

    # get from neo4j using company name
    with driver.session(database="lkinsight") as session:
        if company_name:
            result = session.run("""
                MATCH (c:Company {name: $name})
                OPTIONAL MATCH (c)-[:HAS_PHONE]->(p:Phone)
                OPTIONAL MATCH (c)-[:HAS_EMAIL]->(e:Email)
                WITH c, collect(distinct p.number) as phones, collect(distinct e.address) as emails
                RETURN c.name as name, c.address as address, c.description as description,
                       phones, emails
            """, name=company_name)
        else:
            result = session.run("""
                MATCH (c:Company)
                OPTIONAL MATCH (c)-[:HAS_PHONE]->(p:Phone)
                OPTIONAL MATCH (c)-[:HAS_EMAIL]->(e:Email)
                WITH c, collect(distinct p.number) as phones, collect(distinct e.address) as emails
                RETURN c.name as name, c.address as address, c.description as description,
                       phones, emails
                ORDER BY id(c) DESC
                LIMIT 1
            """)
        record = result.single()

    neo4j_context = ""
    if record:
        neo4j_context = f"""
Company: {record['name']}
Address: {record['address']}
Phone Numbers: {', '.join(record['phones']) if record['phones'] else 'Not found'}
Emails: {', '.join(record['emails']) if record['emails'] else 'Not found'}
Description: {record['description']}
"""

    chroma_context = "\n".join(chroma_results["documents"][0]) if chroma_results["documents"] else ""
    context = neo4j_context + "\n" + chroma_context

    prompt = f"""You are a business info assistant. Answer using the data below. Be brief and direct. If info is not available say "Not found on the website."

Data:
{context}

Question: {input.message}
Answer:"""

    response = ollama.chat(
        model="gemma3:1b",
        messages=[{"role": "user", "content": prompt}]
    )

    return {"response": response["message"]["content"]}

@app.post("/scrape")
def scrape(input: URLInput):
    try:
        text = scrape_with_selenium(input.url)
        raw = extract_business_info(text)
        parsed = parse_llm_output(raw)
        print("PARSED:", parsed)
        if parsed is None:
            return {"status": "error", "message": "LLM failed to return valid JSON"}
        store_in_neo4j(parsed, input.url)
        store_in_chroma(parsed, input.url)
        return {"status": "success", "data": parsed}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
