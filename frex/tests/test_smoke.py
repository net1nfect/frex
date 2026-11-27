def test_list_projects_is_empty(client):
    response = client.get("/projects")
    assert response.status_code == 200
    assert response.json() == []
