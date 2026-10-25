import threading
from fastapi.testclient import TestClient
from pdf2epub.web.server import create_app


def test_restart_requires_local_launcher_and_token(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_worker=False)
    client = TestClient(app, client=("127.0.0.1", 1234))
    assert not client.get('/api/info').json()['can_restart']
    assert client.post('/api/local/restart').status_code == 404
    stopped = threading.Event()
    app.state.restart = stopped.set
    info = client.get('/api/info').json()
    assert info['can_restart']
    assert client.post('/api/local/restart').status_code == 403
    headers = {'X-PDF2EPUB-Instance': info['instance']}
    assert client.post('/api/local/restart', headers={**headers, 'Origin': 'https://untrusted.example'}).status_code == 403
    remote = TestClient(app, client=('192.0.2.1', 1234))
    assert remote.post('/api/local/restart', headers=headers).status_code == 403
    app.state.local_writes = 1
    assert client.post('/api/local/restart', headers=headers).status_code == 409
    app.state.local_writes = 0
    original_query = app.state.db.query
    monkeypatch.setattr(app.state.db, "query", lambda *args: [{"id": "active-job"}])
    assert client.post('/api/local/restart', headers=headers).status_code == 409
    monkeypatch.setattr(app.state.db, "query", original_query)
    assert client.post('/api/local/restart', headers=headers).status_code == 200
    assert stopped.wait(2)
    assert client.post('/api/local/restart', headers=headers).status_code == 503
