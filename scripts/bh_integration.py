#!/usr/bin/env python3
"""Browser Harness → Session-Roles 映射集成

Browser Harness定义了24种persona，session-roles定义了23种角色。
本模块提供映射、路由和bridging能力。
"""
import json, os, sys
from pathlib import Path

BASE = Path.home()
BH_DIR = BASE / "hermes-session-roles" / "personas" / "browser-harness"
SR_DIR = BASE / "hermes-session-roles" / "personas" / "session-roles"

def load_bh_roles() -> dict:
    """Load Browser Harness personas (handles both flat and profiles format)"""
    roles = {}
    for f in sorted(BH_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        data = json.loads(f.read_text())
        if "profiles" in data:
            for pname, pdata in data["profiles"].items():
                roles[pname] = pdata
        else:
            roles[data.get("name", f.stem)] = data
    return roles

def load_sr_roles() -> dict:
    """Load Session Roles"""
    roles = {}
    for f in sorted(SR_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        data = json.loads(f.read_text())
        roles[data.get("name", f.stem)] = data
    return roles

def build_mapping() -> dict:
    """Build BH→SR mapping based on functional overlap"""
    bh = load_bh_roles()
    sr = load_sr_roles()
    
    # Heuristic mapping based on name/title/description overlap
    mapping = {}
    for bh_name, bh_data in bh.items():
        bh_title = bh_data.get("title", "").lower()
        bh_desc = bh_data.get("description", "").lower()
        best_match = None
        best_score = 0
        
        for sr_name, sr_data in sr.items():
            sr_title = sr_data.get("title", "").lower()
            sr_desc = sr_data.get("description", "").lower()
            score = 0
            if bh_name in sr_name or sr_name in bh_name:
                score += 3
            for word in bh_title.split():
                if word in sr_title:
                    score += 1
            for word in bh_desc.split()[:20]:
                if word in sr_desc:
                    score += 0.5
            if score > best_score:
                best_score = score
                best_match = sr_name
        
        mapping[bh_name] = {
            "bh_role": bh_name,
            "mapped_sr": best_match,
            "score": best_score,
            "bh_title": bh_data.get("title", ""),
        }
    
    return mapping

def print_mapping():
    mapping = build_mapping()
    print("# Browser Harness → Session Roles 映射")
    print()
    print("| BH Persona | Mapped SR | Score |")
    print("|------------|-----------|-------|")
    for bh_name, info in sorted(mapping.items()):
        score_tag = "✅" if info["score"] >= 3 else "⚠️" if info["score"] >= 1 else "❌"
        print(f"| {bh_name:<30} | {str(info['mapped_sr']):<15} | {score_tag} {info['score']} |")
    print()
    
    unmapped = [k for k, v in mapping.items() if not v["mapped_sr"]]
    if unmapped:
        print(f"⚠️ Unmapped BH personas: {', '.join(unmapped)}")

if __name__ == "__main__":
    print_mapping()
