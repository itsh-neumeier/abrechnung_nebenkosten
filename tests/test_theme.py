from fastapi.testclient import TestClient

from app.main import app


def test_app_color_and_theme_toggle():
    with TestClient(app) as c:
        c.post("/admin/users/settings", data={"tenant_app_name": "Mein Zuhause", "admin_app_name": "ImmoVerwaltung",
                                              "tenant_app_color": "#C2410C", "admin_app_color": "kaputt"})
        assert c.get("/manifest.webmanifest?app=tenant").json()["theme_color"] == "#c2410c"
        assert c.get("/manifest.webmanifest?app=admin").json()["theme_color"] == "#1e3a8a"  # ungültig → Standard
        page = c.get("/").text
        assert '<meta name="theme-color" content="#c2410c">' in page and "border-top-color:#c2410c" in page
        assert "data-theme-toggle" in page and "<html lang=\"de\">" in page  # automatisch: kein festes Theme
        c.cookies.set("nk_theme", "dark")
        assert '<html lang="de" data-theme="dark">' in c.get("/admin").text
        c.cookies.set("nk_theme", "<script>")
        assert "data-theme=\"<" not in c.get("/").text
