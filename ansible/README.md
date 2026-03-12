# Ansible installer

Два сценария:

- **База**: `playbook-base.yml`
- **База + VLESS**: `playbook-with-vless.yml`

## Локально на шлюзе

```bash
ansible-playbook -i 'localhost,' -c local playbook-base.yml
# или
# ansible-playbook -i 'localhost,' -c local playbook-with-vless.yml
```

## С другой машины (по SSH)

1) Скопируй `inventory.example.ini` в `inventory.ini` и укажи IP/пользователя.
2) Запусти:

```bash
ansible-playbook -i inventory.ini playbook-base.yml
```
