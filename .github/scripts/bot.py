import os, sys, json, urllib.request, urllib.error, base64

TOKEN = os.environ.get("GITHUB_TOKEN")
REPO = os.environ.get("REPO")
PR_NUMBER = os.environ.get("PR_NUMBER")
AUTHOR = os.environ.get("PR_AUTHOR")

API_BASE = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

def api_request(url, method="GET", data=None):
    req = urllib.request.Request(url, headers=HEADERS, method=method)
    if data:
        req.data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            res = response.read()
            return json.loads(res.decode('utf-8')) if res else {}
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8')}")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

def close_with_comment(msg):
    print(f"Declining PR: {msg}")
    api_request(f"{API_BASE}/issues/{PR_NUMBER}/comments", method="POST", data={"body": f"❌ **Automated Validation Failed**\n\n{msg}"})
    api_request(f"{API_BASE}/pulls/{PR_NUMBER}", method="PATCH", data={"state": "closed"})
    sys.exit(1)

def main():
    print(f"Processing PR #{PR_NUMBER} by {AUTHOR}")
    
    # 1. Fetch PR details for the head sha
    pr_data = api_request(f"{API_BASE}/pulls/{PR_NUMBER}")
    if not pr_data:
        sys.exit(1)
        
    head_sha = pr_data["head"]["sha"]
    
    # 2. Fetch modified files
    files = api_request(f"{API_BASE}/pulls/{PR_NUMBER}/files")
    if not files:
        sys.exit(1)
        
    flac_files = []
    for f in files:
        filename = f.get("filename", "")
        if filename.startswith("Music/") and filename.endswith(".flac"):
            flac_files.append((filename, f["sha"]))
            
    if not flac_files:
        close_with_comment("Your pull request must include a `.flac` file in the `Music/` directory.")
        
    for filename, sha in flac_files:
        basename = filename.split("/")[-1].lower()
        if not basename.startswith(AUTHOR.lower()):
            close_with_comment(f"Your audio file `{filename}` must be prefixed with your GitHub username (`{AUTHOR}-...`).")
            
    # 3. Fetch their version of music.json to extract the song
    file_data = api_request(f"{API_BASE}/contents/music.json?ref={head_sha}")
    if not file_data or "content" not in file_data:
        close_with_comment("Could not read `music.json` from your pull request.")
        
    content = base64.b64decode(file_data["content"]).decode("utf-8")
    
    objects = []
    try:
        full_json = json.loads(content)
        objects = full_json.get("items", [])
    except json.JSONDecodeError:
        # Try raw decode trick if they mangled the JSON wrapping
        inner = content
        idx = inner.find('[')
        if idx != -1: inner = inner[idx+1:]
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(inner):
            idx = inner.find('{', idx)
            if idx == -1: break
            try:
                obj, end_idx = decoder.raw_decode(inner[idx:])
                if isinstance(obj, dict) and "song" in obj and "artist" in obj and "url" in obj:
                    objects.append(obj)
                idx += end_idx
            except json.JSONDecodeError:
                idx += 1
                
    if not objects:
        close_with_comment("Failed to parse any valid song entries from your `music.json`. Please ensure it is valid JSON.")
        
    # Extract only the newly added songs belonging to this author
    new_songs = []
    for obj in objects:
        if not isinstance(obj, dict) or "song" not in obj or "artist" not in obj or "url" not in obj:
            continue
        if not obj["song"].strip() or not obj["artist"].strip() or not obj["url"].strip():
            continue
        filename = obj["url"].split("/")[-1]
        if filename.lower().startswith(AUTHOR.lower()):
            new_songs.append(obj)
            
    if not new_songs:
        close_with_comment("Could not find any new valid song entries in `music.json` that match your username prefix.")

    # 4. Fetch the CURRENT music.json from main
    main_ref = api_request(f"{API_BASE}/git/ref/heads/main")
    main_commit_sha = main_ref["object"]["sha"]
    
    main_file = api_request(f"{API_BASE}/contents/music.json")
    main_content = base64.b64decode(main_file["content"]).decode("utf-8")
    main_db = json.loads(main_content)
    
    # Deduplicate against existing
    seen = { (i["song"].lower(), i["artist"].lower()) for i in main_db["items"] }
    valid_new = []
    for s in new_songs:
        key = (s["song"].lower(), s["artist"].lower())
        if key not in seen:
            seen.add(key)
            valid_new.append(s)
            
    if not valid_new:
        close_with_comment("The song you submitted is already in the database!")
        
    # Prepend the new songs to the top (like the website does)
    for s in reversed(valid_new):
        main_db["items"].insert(0, s)
        
    new_music_content = json.dumps(main_db, indent=2, ensure_ascii=False)
    
    # 5. Build Tree
    tree = [{"path": "music.json", "mode": "100644", "type": "blob", "content": new_music_content}]
    for path, sha in flac_files:
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
        
    print("Creating git tree...")
    new_tree = api_request(f"{API_BASE}/git/trees", method="POST", data={"base_tree": main_commit_sha, "tree": tree})
    if not new_tree:
        close_with_comment("Failed to create git tree due to a backend error.")
        
    print("Creating commit...")
    commit_data = {
        "message": f"Auto-merge PR #{PR_NUMBER} by {AUTHOR}",
        "tree": new_tree["sha"],
        "parents": [main_commit_sha]
    }
    new_commit = api_request(f"{API_BASE}/git/commits", method="POST", data=commit_data)
    
    print("Updating main ref...")
    api_request(f"{API_BASE}/git/refs/heads/main", method="PATCH", data={"sha": new_commit["sha"]})
    
    # 6. Success! Close the PR.
    api_request(f"{API_BASE}/issues/{PR_NUMBER}/comments", method="POST", data={"body": f"✅ **Successfully Auto-Merged!**\n\nYour contribution was flawlessly merged into `main` via commit {new_commit['sha']}."})
    api_request(f"{API_BASE}/pulls/{PR_NUMBER}", method="PATCH", data={"state": "closed"})
    print("Done!")

if __name__ == "__main__":
    main()
