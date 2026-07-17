import json
import subprocess
import sys

def get_services():
    try:
        # Get all services and their descriptions
        result = subprocess.run(
            ['systemctl', 'list-units', '--type=service', '--state=running', '--no-legend', '--no-pager'],
            capture_output=True, text=True, check=True
        )
        services = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 5:
                unit = parts[0]
                description = parts[4]
                services.append({
                    "systemd_unit": unit,
                    "process_name": unit.replace('.service', ''),
                    "display_name": description,
                    "runtime_state": "running",
                    "is_active": True
                })
        return services
    except Exception as e:
        return []

if __name__ == "__main__":
    try:
        print(json.dumps(get_services()))
    except Exception:
        print("[]")
