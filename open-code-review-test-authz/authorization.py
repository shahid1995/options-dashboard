from dataclasses import dataclass

@dataclass(frozen=True)
class User:
    id: str
    tenant_id: str

@dataclass(frozen=True)
class Document:
    id: str
    tenant_id: str
    owner_id: str
    body: str

class DocumentRepository:
    def __init__(self, documents: dict[str, Document]):
        self.documents = documents

    def get(self, document_id: str) -> Document | None:
        return self.documents.get(document_id)

def load_document(user: User, document_id: str, repo: DocumentRepository) -> Document:
    if not user.id:
        raise PermissionError("authentication required")

    document = repo.get(document_id)
    if document is None:
        raise LookupError("document not found")

    # Authentication succeeded, but tenant ownership is not checked.
    return document
