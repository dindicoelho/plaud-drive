#!/usr/bin/env python3
"""
Setup do Google Drive para um usuário.
Roda uma vez por pessoa — abre o browser, faz login, salva credenciais.

Uso:
    python setup_drive.py nome-da-pessoa

Exemplo:
    python setup_drive.py dindi
    python setup_drive.py namorada
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main():
    if len(sys.argv) < 2:
        print("Uso: python setup_drive.py <nome>")
        print("Exemplo: python setup_drive.py dindi")
        sys.exit(1)

    name = sys.argv[1].lower().strip()
    users_dir = Path(__file__).parent / "users"
    save_path = str(users_dir / f"{name}_drive_creds.json")

    if os.path.exists(save_path):
        print(f"Credenciais já existem em {save_path}")
        resp = input("Quer refazer? (s/n): ")
        if resp.lower() != "s":
            return

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("❌ GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET não encontrados no .env")
        print()
        print("Pra criar:")
        print("1. Vai em https://console.cloud.google.com/apis/credentials")
        print("2. Cria um projeto (ou usa um existente)")
        print("3. Ativa a Google Drive API")
        print("4. Cria credencial OAuth 2.0 tipo 'Desktop App'")
        print("5. Copia Client ID e Client Secret pro .env")
        sys.exit(1)

    from drive_client import setup_user_drive
    setup_user_drive(client_id, client_secret, save_path)
    print(f"✅ Google Drive configurado para {name}!")


if __name__ == "__main__":
    main()
