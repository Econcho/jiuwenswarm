#!/usr/bin/env python3
import os
import sys
import json
import secrets
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
from calculate_hash import calculate_project_hash
from upload_zip import upload_skill_with_size

MAX_QUESTION_LENGTH = 2000

ENV_FILE_PATH = '/home/sandbox/.openclaw/.xiaoyienv'
API_URL_SUFFIX = '/celia-claw/v1/rest-api/skill/execute'
SKILL_ID = 'skill-scope'

class EnvConfig:
    def __init__(self):
        self.apiKey = ''
        self.uid = ''
        self.serviceUrl = ''

def load_env_config() -> EnvConfig:
    config = EnvConfig()
    
    try:
        with open(ENV_FILE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                trimmed = line.strip()
                if not trimmed or trimmed.startswith('#'):
                    continue
                parts = trimmed.split('=', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip()
                    if key == 'PERSONAL-API-KEY':
                        config.apiKey = value
                    elif key == 'PERSONAL-UID':
                        config.uid = value
                    elif key == 'SERVICE_URL':
                        config.serviceUrl = value
    except:
        pass
    
    return config

def load_skill_content(skill_md_path: str) -> str:
    try:
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            return f.read()
    except:
        return ""

def build_question_text(
    target_hash: str,
    download_url: str = "",
    skill_type: str = "",
    source: str = "",
    skill_content: str = "",
    file_size: int = 0
) -> str:
    data = {
        "subSceneID": "AI_AGENT_RUNTIME_XIAOYICLAW_SKILL_SCAN",
        "hash": target_hash,
        "url": download_url,
        "size": file_size,
        "type": skill_type,
        "source": source,
        "content": ""
    }
    
    base_json = json.dumps({
        "subSceneID": data["subSceneID"],
        "hash": data["hash"],
        "url": data["url"],
        "size": data["size"],
        "type": data["type"],
        "source": data["source"]
    })
    base_length = len(base_json)
    available = MAX_QUESTION_LENGTH - base_length
    
    if skill_content and available > 0:
        if len(skill_content) > available:
            skill_content = skill_content[:available]
        data["content"] = skill_content
    
    question_text = json.dumps(data)
    return question_text

def generate_trace_id() -> str:
    return secrets.token_hex(16)

def call_as_api(
    target_hash: str,
    download_url: str = "",
    skill_type: str = "",
    source: str = "",
    skill_content: str = "",
    file_size: int = 0
) -> str:
    env_config = load_env_config()
    question_text = build_question_text(target_hash, download_url, skill_type, source, skill_content, file_size)
    
    if not env_config.serviceUrl:
        raise Exception("SERVICE_URL is not configured")
    
    api_url = env_config.serviceUrl + API_URL_SUFFIX
    trace_id = generate_trace_id()
    
    headers = {
        'x-hag-trace-id': trace_id,
        'sessionId': trace_id,
        'x-uid': env_config.uid,
        'x-api-key': env_config.apiKey,
        'x-request-from': 'openclaw',
        'x-skill-id': SKILL_ID,
        'Content-Type': 'application/json'
    }
    
    body = json.dumps({
        "questionText": question_text,
        "textSource": "question",
        "action": "SKILL_SCAN",
        "extra": json.dumps({
            "userId": env_config.uid
        })
    })
    
    try:
        req = urllib.request.Request(
            api_url,
            data=body.encode('utf-8'),
            headers=headers,
            method='POST'
        )
        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        raise Exception(f"API request failed with status {e.code}")
    except Exception as e:
        raise Exception(f"API request failed: {e}")

def main():
    args = sys.argv[1:]
    
    if len(args) < 3:
        print("Error: skill_path, type, and source are required.", file=sys.stderr)
        print("Usage: python3 check.py <skill_path> <type> <source>", file=sys.stderr)
        print("  type: upload | download | create", file=sys.stderr)
        print("  source: URL or command", file=sys.stderr)
        sys.exit(2)
    
    skill_path = args[0]
    skill_type = args[1]
    source = args[2]
    
    print(f"[Skill Scope] Starting security scan for: {skill_path}")
    
    target_hash = calculate_project_hash(skill_path)
    print(f"[Skill Scope] Hash calculated: {target_hash}")
    
    download_url, file_size = upload_skill_with_size(skill_path)
    print(f"[Skill Scope] Skill uploaded, URL: {download_url}, Size: {file_size} bytes")
    
    skill_md_path = os.path.join(skill_path, 'SKILL.md')
    skill_content = load_skill_content(skill_md_path)
    
    response_text = ""
    try:
        response_text = call_as_api(target_hash, download_url, skill_type, source, skill_content, file_size)
    except Exception as e:
        print(f"[Skill Scope] Failed to call API: {e}", file=sys.stderr)
        sys.exit(3)
    
    try:
        result = json.loads(response_text)
        data = result.get('data', {})
        security_result = data.get('securityResult', '') if isinstance(data, dict) else ''
        
        if security_result == "ACCEPT":
            print("[Skill Scope] Benign: Scan completed, verification passed.")
            sys.exit(0)
        elif security_result == "REJECT":
            print("[Skill Scope] Malicious: Malicious Skill detected! This skill poses a serious security threat.", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"[Skill Scope] Unknown security result", file=sys.stderr)
            sys.exit(2)
    except Exception as e:
        print(f"[Skill Scope] Failed to parse response: {e}", file=sys.stderr)
        sys.exit(3)

if __name__ == "__main__":
    main()