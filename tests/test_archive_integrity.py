from pathlib import Path
import os
os.chdir(Path(__file__).resolve().parents[1])

def test_required_files_exist():
    required = [
        "Dockerfile.graph",
        "Dockerfile.community-worker",
        "Dockerfile.discord",
        "Dockerfile.community-eval",
        "docker-compose.yml",
        "src/db_migrate.py",
        "src/db_roles.py",
        "src/fc27_community_provider.py",
        "src/fc27_polling_worker.py",
        "src/fc27_discord_bot.py",
        "src/fc27_match_results.py",
    ]
    assert not [name for name in required if not Path(name).is_file()]

def test_readme_references_existing_documents():
    text = Path("README.md").read_text(encoding="utf-8")
    for name in [
        "config.example.env",
        "docker-compose.yml",
        "docs/ARCHITECTURE.md",
        "docs/FC27_COMPONENT_STATUS.md",
    ]:
        assert name in text
        assert Path(name).exists()
