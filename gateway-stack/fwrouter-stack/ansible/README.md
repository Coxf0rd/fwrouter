# Ansible (optional)

This folder contains a minimal installer to copy:

- `host-sbin/` -> `/usr/local/sbin/`
- `host-systemd/` -> `/etc/systemd/system/`
- `host-etc-fwrouter/` -> `/etc/fwrouter/` (examples only; does not overwrite existing by default)

It is intentionally conservative (gateway machine).

## Usage

1) Install Ansible on your admin machine.
2) Copy `inventory.example.ini` to `inventory.ini` and set `ansible_host`.
3) Run:

```bash
ansible-playbook -i inventory.ini playbook.yml
```
