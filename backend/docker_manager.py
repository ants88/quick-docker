import subprocess

import docker
from docker.errors import DockerException, NotFound


class DockerManager:
    def __init__(self):
        self.client = docker.from_env()

    def close(self):
        self.client.close()

    def _container_info(self, c) -> dict:
        labels = c.labels or {}
        ports = {}
        for container_port, bindings in (c.ports or {}).items():
            if bindings:
                ports[container_port] = bindings[0].get("HostPort", "")
        return {
            "id": c.short_id,
            "full_id": c.id,
            "name": c.name,
            "image": str(c.image.tags[0]) if c.image.tags else str(c.image.short_id),
            "status": c.status,
            "state": c.attrs.get("State", {}).get("Status", c.status),
            "ports": ports,
            "compose_project": labels.get("com.docker.compose.project", ""),
            "compose_service": labels.get("com.docker.compose.service", ""),
            "compose_workdir": labels.get("com.docker.compose.project.working_dir", ""),
        }

    def list_containers(self) -> list[dict]:
        containers = self.client.containers.list(all=True)
        return [self._container_info(c) for c in containers]

    def list_projects(self) -> list[dict]:
        containers = self.list_containers()
        projects: dict[str, dict] = {}

        for c in containers:
            project_name = c["compose_project"] or "_standalone"
            if project_name not in projects:
                projects[project_name] = {
                    "name": project_name,
                    "workdir": c["compose_workdir"],
                    "containers": [],
                    "status": "stopped",
                }
            projects[project_name]["containers"].append(c)

        for p in projects.values():
            states = [c["status"] for c in p["containers"]]
            if all(s == "running" for s in states):
                p["status"] = "running"
            elif any(s == "running" for s in states):
                p["status"] = "partial"
            else:
                p["status"] = "stopped"

        return sorted(projects.values(), key=lambda p: (p["name"] == "_standalone", p["name"]))

    def compose_action(self, project: str, action: str) -> dict:
        projects = {p["name"]: p for p in self.list_projects()}
        if project not in projects:
            return {"ok": False, "error": f"Project '{project}' not found"}

        workdir = projects[project].get("workdir")
        if not workdir:
            return {"ok": False, "error": f"No workdir for project '{project}'"}

        cmd = ["docker", "compose", action]
        if action == "up":
            cmd.append("-d")

        result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}
        return {"ok": True, "output": result.stdout.strip()}

    def container_action(self, container_id: str, action: str) -> dict:
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"ok": False, "error": f"Container '{container_id}' not found"}

        try:
            if action == "start":
                container.start()
            elif action == "stop":
                container.stop(timeout=10)
            elif action == "restart":
                container.restart(timeout=10)
            elif action == "remove":
                container.remove(force=True)
            else:
                return {"ok": False, "error": f"Unknown action '{action}'"}
        except DockerException as e:
            return {"ok": False, "error": str(e)}

        return {"ok": True}

    def container_logs(self, container_id: str, tail: int = 200):
        container = self.client.containers.get(container_id)
        return container.logs(stream=True, follow=True, tail=tail)

    def container_exec(self, container_id: str, cols: int = 80, rows: int = 24):
        container = self.client.containers.get(container_id)
        exec_id = self.client.api.exec_create(
            container.id,
            cmd="/bin/sh",
            stdin=True,
            tty=True,
            stdout=True,
            stderr=True,
            environment={"TERM": "xterm-256color", "COLUMNS": str(cols), "ROWS": str(rows)},
        )
        sock = self.client.api.exec_start(exec_id["Id"], socket=True, tty=True)
        return exec_id["Id"], sock

    def exec_resize(self, exec_id: str, cols: int, rows: int):
        self.client.api.exec_resize(exec_id, height=rows, width=cols)

    def health_check(self) -> dict:
        try:
            self.client.ping()
            info = self.client.info()
            return {
                "ok": True,
                "containers": info.get("Containers", 0),
                "images": info.get("Images", 0),
                "server_version": info.get("ServerVersion", "unknown"),
            }
        except DockerException as e:
            return {"ok": False, "error": str(e)}
