import io
import json
import os

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveClient:
    def __init__(self, creds_path: str):
        """Inicializa com o path do arquivo de credenciais salvas do usuário."""
        self.creds_path = creds_path
        self.service = self._authenticate()
        self._folder_cache: dict[str, str] = {}

    def _authenticate(self):
        creds = None
        if os.path.exists(self.creds_path):
            creds = Credentials.from_authorized_user_file(self.creds_path, SCOPES)

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save_creds(creds)

        if not creds or not creds.valid:
            raise Exception(
                f"Credenciais inválidas ou ausentes em {self.creds_path}. "
                "Execute setup_drive.py primeiro."
            )

        return build("drive", "v3", credentials=creds)

    def _save_creds(self, creds: Credentials):
        with open(self.creds_path, "w") as f:
            f.write(creds.to_json())

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Retorna o ID da pasta, criando se não existir."""
        cache_key = f"{parent_id or 'root'}:{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        # Busca pasta existente
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"

        results = self.service.files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=1
        ).execute()

        files = results.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            if parent_id:
                metadata["parents"] = [parent_id]
            folder = self.service.files().create(body=metadata, fields="id").execute()
            folder_id = folder["id"]

        self._folder_cache[cache_key] = folder_id
        return folder_id

    def upload_markdown(self, filename: str, content: str, folder_id: str) -> str:
        """Faz upload de um arquivo .md para uma pasta. Retorna o file ID."""
        # Verifica se já existe (evita duplicatas)
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        results = self.service.files().list(
            q=query, spaces="drive", fields="files(id)", pageSize=1
        ).execute()

        media = MediaInMemoryUpload(
            content.encode("utf-8"), mimetype="text/markdown", resumable=False
        )

        existing = results.get("files", [])
        if existing:
            # Atualiza o existente
            file = self.service.files().update(
                fileId=existing[0]["id"], media_body=media
            ).execute()
            return existing[0]["id"]

        # Cria novo
        metadata = {"name": filename, "parents": [folder_id]}
        file = self.service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        return file["id"]

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        """Lista todos os arquivos de uma pasta."""
        results = self.service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces="drive",
            fields="files(id, name, mimeType)",
            orderBy="name",
            pageSize=100,
        ).execute()
        return results.get("files", [])

    def read_file(self, file_id: str) -> str:
        """Lê o conteúdo de um arquivo de texto."""
        content = self.service.files().get_media(fileId=file_id).execute()
        return content.decode("utf-8")

    def list_client_folders(self, root_folder_id: str) -> list[dict]:
        """Lista subpastas (clientes) dentro da pasta raiz."""
        query = (
            f"'{root_folder_id}' in parents "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        )
        results = self.service.files().list(
            q=query, spaces="drive", fields="files(id, name)", orderBy="name", pageSize=100
        ).execute()
        return results.get("files", [])


def setup_user_drive(client_id: str, client_secret: str, save_path: str):
    """Fluxo de autenticação OAuth2 para um novo usuário. Abre o browser."""
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "w") as f:
        f.write(creds.to_json())

    print(f"Credenciais salvas em {save_path}")
    return creds
