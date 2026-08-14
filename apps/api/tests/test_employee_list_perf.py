"""Danh sách NV không stat ảnh từng dòng — tránh chậm khi 300+ NV."""

from unittest.mock import patch

from app.core.config import get_settings


def _hr_headers(client):
    token = client.post(
        "/api/auth/login", json={"username": "hr.demo", "password": "HrDemo@123456"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_list_employees_skips_photo_stat(client):
    with patch("app.modules.mdm.service._employee_photo_file") as photo_stat:
        res = client.get("/api/employees", headers=_hr_headers(client))
        assert res.status_code == 200
        assert len(res.json()) >= 1
        photo_stat.assert_not_called()
