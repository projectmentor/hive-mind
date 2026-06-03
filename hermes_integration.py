"""
Hive Mind Memory Provider for Hermes

Integrates Hermes with the institutional hive-mind memory system.
"""

import subprocess
import json
import os
from pathlib import Path

class HiveMemoryProvider:
    """Memory provider that delegates to hive CLI"""
    
    def __init__(self):
        self.hv_path = Path(os.environ.get("HIVE_HOME", Path.home() / "projects" / "hive-mind")) / "hv"
        if not self.hv_path.exists():
            raise Exception(f"Hive CLI not found at {self.hv_path}")
    
    def add(self, content: str, target: str = "memory") -> dict:
        """Store memory via hive CLI"""
        cmd = [
            str(self.hv_path),
            "remember", 
            content,
            "--tags", target,
            "--source", "hermes"
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                return {"success": True, "message": result.stdout.strip()}
            else:
                return {"success": False, "error": result.stderr.strip()}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def replace(self, old_content: str, new_content: str, target: str = "memory") -> dict:
        """Replace memory (for now, just add new)"""
        return self.add(new_content, target)
    
    def remove(self, content: str, target: str = "memory") -> dict:
        """Remove memory (not implemented in hive yet)"""
        return {"success": True, "message": "Remove not implemented yet"}
    
    def search(self, query: str) -> list:
        """Search hive memory"""
        cmd = [
            str(self.hv_path),
            "search",
            query,
            "--format", "json"
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                return json.loads(result.stdout or "[]")
            else:
                return []
        except Exception as e:
            return []

# For direct usage
def hive_remember(content: str, target: str = "memory") -> dict:
    provider = HiveMemoryProvider()
    return provider.add(content, target)

def hive_search(query: str) -> list:
    provider = HiveMemoryProvider()
    return provider.search(query)

if __name__ == "__main__":
    # Test the provider
    provider = HiveMemoryProvider()
    
    print("Testing hive memory provider...")
    
    # Test remember
    result = provider.add("Test fact from Hermes integration", "test")
    print(f"Remember result: {result}")
    
    # Test search
    facts = provider.search("test")
    print(f"Search results: {len(facts)} facts found")
    for fact in facts[:3]:  # Show first 3
        print(f"  - {fact.get('content', '')}")