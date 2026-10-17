#!/usr/bin/env python3
import gzip
import json
import random
from pathlib import Path

def main():
    print("Reading valid org numbers from brreg-enheter.csv.gz...")
    valid_orgs = set()
    with gzip.open("brreg-enheter.csv.gz", "rt", encoding="utf-8") as f:
        header = f.readline()
        # Find index of organisasjonsnummer
        cols = header.strip().split(",")
        org_idx = cols.index('"organisasjonsnummer"') if '"organisasjonsnummer"' in cols else 0
        for line in f:
            if not line.strip(): continue
            valid_orgs.add(line.split(",")[org_idx].strip('"'))
            
    print(f"Found {len(valid_orgs)} valid orgs.")
    
    print("Reading universe...")
    with gzip.open("signalpost-universe.jsonl.gz", "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
        
    print(f"Universe size: {len(rows)}")
    valid_rows = [r for r in rows if r["organisation_number"] in valid_orgs]
    print(f"Valid universe size: {len(valid_rows)}")
    
    chosen = random.Random(2026).sample(valid_rows, 1000)
    
    with open("entry-companies.jsonl", "w", encoding="utf-8") as f:
        for row in chosen:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            
    print("Successfully wrote 1000 guaranteed valid companies to entry-companies.jsonl")

if __name__ == "__main__":
    main()
