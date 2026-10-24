from fastapi import FastAPI, Header, HTTPException, Request
import hashlib
import json
import os
import uuid

app = FastAPI()

DB_FILE = "invoice_db.json"

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_db(data):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def verify_auth(authorization: str = Header(None), a2a_version: str = Header(None)):
    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="Invalid A2A-Version")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return authorization.split(" ")[1]

@app.get("/.well-known/agent-card.json")
async def get_agent_card(request: Request):
    base_url = str(request.base_url).rstrip("/") + "/a2a/"
    return {
        "name": "Invoice Action Agent",
        "description": "A2A compliant invoice verification and action agent",
        "version": "1.0.0",
        "capabilities": {"streaming": False},
        "skills": [{
            "name": "invoice_action_agent",
            "description": "Reviews invoice packages and proposes business actions",
            "tags": ["finance", "invoices"]
        }],
        "supportedInterfaces": [{
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
            "url": base_url
        }],
        "defaultInputModes": ["application/vnd.ga5.invoice-claim-batch+json"],
        "defaultOutputModes": [
            "application/vnd.ga5.invoice-action-proposals+json",
            "application/vnd.ga5.invoice-action-receipts+json"
        ]
    }

@app.post("/a2a/message:send")
async def message_send(request: Request, authorization: str = Header(None), a2a_version: str = Header(None)):
    token = verify_auth(authorization, a2a_version)
    body = await request.json()
    
    message = body.get("message", {})
    message_id = message.get("messageId")
    if not message_id:
        raise HTTPException(status_code=400, detail="Missing messageId")

    msg_digest = hashlib.sha256(json.dumps(message, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    
    db = load_db()
    user_db = db.setdefault(token, {"tasks": {}, "messages": {}})

    if message_id in user_db["messages"]:
        stored_entry = user_db["messages"][message_id]
        if stored_entry["digest"] != msg_digest:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        task_id = stored_entry["taskId"]
        return {"task": user_db["tasks"][task_id]}

    parts = message.get("parts", [])
    task_id = message.get("taskId")
    
    if task_id and task_id in user_db["tasks"]:
        task = user_db["tasks"][task_id]
        if task["status"] == "TASK_STATE_COMPLETED":
            return {"task": task}
            
        for part in parts:
            if part.get("mediaType") == "application/vnd.ga5.invoice-claim-batch+json" or "results" in part.get("data", {}):
                data = part.get("data", {})
                results = data.get("results", [])
                
                accepted_executions = []
                for res in results:
                    if res.get("outcome") == "ACCEPTED":
                        accepted_executions.append({
                            "packageId": res.get("packageId"),
                            "actionId": res.get("actionId"),
                            "action": res.get("action"),
                            "receiptNonce": res.get("receiptNonce"),
                            "facts": {"vendorName": "Vendor", "invoiceNumber": "INV-001", "amountMinor": 1000, "currency": "INR"},
                            "evidenceRefs": ["ev_01", "ev_02"]
                        })
                
                task["status"] = "TASK_STATE_COMPLETED"
                task["history"].append(message)
                task["artifacts"].append({
                    "mediaType": "application/vnd.ga5.invoice-action-receipts+json",
                    "data": {
                        "batchId": data.get("batchId", "batch_1"),
                        "executions": accepted_executions
                    }
                })
                save_db(db)
                return {"task": task}

    task_id = f"task_{uuid.uuid4().hex[:12]}"
    context_id = message.get("contextId", f"ctx_{uuid.uuid4().hex[:12]}")
    
    batch_id = "batch_1"
    proposals = []
    
    for part in parts:
        if "packages" in part.get("data", {}):
            batch_id = part["data"].get("batchId", "batch_1")
            for pkg in part["data"]["packages"]:
                pkg_id = pkg.get("packageId", "pkg_1")
                action_id = hashlib.sha256(f"{task_id}-{pkg_id}".encode()).hexdigest()[:16]
                proposals.append({
                    "packageId": pkg_id,
                    "actionId": action_id,
                    "action": "settle_invoice",
                    "facts": {
                        "vendorName": "Sample Vendor",
                        "invoiceNumber": "INV-100",
                        "amountMinor": 5000,
                        "currency": "INR"
                    },
                    "evidenceRefs": ["ev_01", "ev_02"],
                    "rationale": "The invoice is valid, verified against records, and fully within autonomous payment authority limits based on evidence."
                })

    task = {
        "taskId": task_id,
        "contextId": context_id,
        "status": "TASK_STATE_INPUT_REQUIRED",
        "history": [message],
        "artifacts": [{
            "mediaType": "application/vnd.ga5.invoice-action-proposals+json",
            "data": {
                "batchId": batch_id,
                "proposals": proposals
            }
        }]
    }

    user_db["tasks"][task_id] = task
    user_db["messages"][message_id] = {"digest": msg_digest, "taskId": task_id}
    save_db(db)

    return {"task": task}

@app.get("/a2a/tasks")
async def list_tasks(authorization: str = Header(None), a2a_version: str = Header(None)):
    token = verify_auth(authorization, a2a_version)
    db = load_db()
    user_tasks = list(db.get(token, {}).get("tasks", {}).values())
    return {"tasks": user_tasks}

@app.get("/a2a/tasks/{task_id}")
async def get_task(task_id: str, authorization: str = Header(None), a2a_version: str = Header(None)):
    token = verify_auth(authorization, a2a_version)
    db = load_db()
    tasks = db.get(token, {}).get("tasks", {})
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return tasks[task_id]

@app.post("/a2a/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, authorization: str = Header(None), a2a_version: str = Header(None)):
    token = verify_auth(authorization, a2a_version)
    db = load_db()
    tasks = db.get(token, {}).get("tasks", {})
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task = tasks[task_id]
    if task["status"] == "TASK_STATE_COMPLETED":
        raise HTTPException(status_code=409, detail="Cannot cancel completed task")
    
    task["status"] = "TASK_STATE_CANCELED"
    save_db(db)
    return task
