from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class EnvironmentResource:
    id: str
    kind: str
    label: str
    location: str
    status: str
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "location": self.location,
            "status": self.status,
            "detail": self.detail,
            "metadata": self.metadata,
        }


@dataclass
class EnvironmentSurface:
    id: str
    kind: str
    label: str
    location: str
    status: str
    reachability: str
    trust_level: str
    confirmation_policy: str
    detail: str = ""
    operations: list[str] = field(default_factory=list)
    backed_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "location": self.location,
            "status": self.status,
            "reachability": self.reachability,
            "trust_level": self.trust_level,
            "confirmation_policy": self.confirmation_policy,
            "detail": self.detail,
            "operations": list(self.operations),
            "backed_by": list(self.backed_by),
            "metadata": self.metadata,
        }


@dataclass
class EnvironmentLocation:
    id: str
    label: str
    kind: str
    status: str
    summary: str
    reachability: str
    resources: list[str] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "reachability": self.reachability,
            "resources": list(self.resources),
            "surfaces": list(self.surfaces),
            "metadata": self.metadata,
        }


@dataclass
class ConnectedAccount:
    id: str
    provider: str
    label: str
    service: str
    status: str
    location: str
    reachable: str
    detail: str = ""
    identity_hint: str = ""
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "service": self.service,
            "status": self.status,
            "location": self.location,
            "reachable": self.reachable,
            "detail": self.detail,
            "identity_hint": self.identity_hint,
            "capabilities": list(self.capabilities),
            "metadata": self.metadata,
        }


@dataclass
class EnvironmentModel:
    created_at: str
    summary: dict[str, Any]
    locations: list[EnvironmentLocation]
    resources: list[EnvironmentResource]
    surfaces: list[EnvironmentSurface]
    accounts: list[ConnectedAccount]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "summary": self.summary,
            "locations": [item.to_dict() for item in self.locations],
            "resources": [item.to_dict() for item in self.resources],
            "surfaces": [item.to_dict() for item in self.surfaces],
            "accounts": [item.to_dict() for item in self.accounts],
            "diagnostics": self.diagnostics,
        }

    def prompt_block(self) -> str:
        lines = ["## Environment Model"]
        headline = self.summary.get("headline")
        if headline:
            lines.append(headline)
        for loc in self.locations:
            lines.append(f"- {loc.label}: {loc.summary}")
        if self.accounts:
            connected = []
            for acct in self.accounts:
                if acct.status in {"connected", "configured", "active"}:
                    label = acct.label
                    if acct.identity_hint:
                        label += f" ({acct.identity_hint})"
                    connected.append(label)
            if connected:
                lines.append("Connected services: " + ", ".join(connected))
        return "\n".join(lines)

    def tool_context_block(self) -> str:
        lines = ["Environment surfaces relevant to tool choice:"]
        for surface in self.surfaces[:12]:
            ops = ", ".join(surface.operations[:4]) if surface.operations else "general access"
            lines.append(
                f"- {surface.label} @ {surface.location}: status={surface.status}, "
                f"reachability={surface.reachability}, trust={surface.trust_level}, "
                f"confirm={surface.confirmation_policy}, ops={ops}"
            )
        if self.accounts:
            lines.append("Connected accounts/services:")
            for acct in self.accounts[:8]:
                lines.append(
                    f"- {acct.label}: status={acct.status}, service={acct.service}, reachable={acct.reachable}"
                )
        return "\n".join(lines)


class EnvironmentModelService:
    """Builds a structured view of the entity's current surrounding environment."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cfg = cfg
        self._topology = None
        self._runtime_discovery = None
        self._workspace_service = None
        self._computer_use_service = None
        self._capability_registry = None
        self._tool_registry = None

    def wire(
        self,
        *,
        topology=None,
        runtime_discovery=None,
        workspace_service=None,
        computer_use_service=None,
        capability_registry=None,
        tool_registry=None,
    ) -> None:
        self._topology = topology
        self._runtime_discovery = runtime_discovery
        self._workspace_service = workspace_service
        self._computer_use_service = computer_use_service
        self._capability_registry = capability_registry
        self._tool_registry = tool_registry

    def build_model(self) -> EnvironmentModel:
        locations: list[EnvironmentLocation] = []
        resources: list[EnvironmentResource] = []
        surfaces: list[EnvironmentSurface] = []
        accounts: list[ConnectedAccount] = []

        locations.extend(self._workspace_locations(resources, surfaces))
        locations.extend(self._desktop_locations(resources, surfaces))
        locations.extend(self._service_locations(resources, surfaces, accounts))
        self._append_toolpack_surfaces(surfaces)
        self._refresh_location_links(locations, resources, surfaces)

        summary = self._build_summary(locations, resources, surfaces, accounts)
        diagnostics = {
            "runtime_discovery": self._runtime_discovery.to_dict() if self._runtime_discovery is not None else None,
            "topology": self._topology.status_summary() if self._topology is not None else None,
            "capabilities": self._capability_registry.health_summary() if self._capability_registry is not None else None,
        }
        return EnvironmentModel(
            created_at=_now_iso(),
            summary=summary,
            locations=locations,
            resources=resources,
            surfaces=surfaces,
            accounts=accounts,
            diagnostics=diagnostics,
        )

    def _refresh_location_links(
        self,
        locations: list[EnvironmentLocation],
        resources: list[EnvironmentResource],
        surfaces: list[EnvironmentSurface],
    ) -> None:
        for loc in locations:
            loc.resources = [res.id for res in resources if res.location == loc.id]
            loc.surfaces = [surface.id for surface in surfaces if surface.location == loc.id]

    def _workspace_locations(
        self,
        resources: list[EnvironmentResource],
        surfaces: list[EnvironmentSurface],
    ) -> list[EnvironmentLocation]:
        if self._workspace_service is None:
            return [
                EnvironmentLocation(
                    id="workspace",
                    label="workspace",
                    kind="filesystem",
                    status="unavailable",
                    summary="persistent workspace not initialized",
                    reachability="none",
                )
            ]

        state = self._workspace_service.state()
        root = self._workspace_service.root_path()
        ctx_docs = list(getattr(state, "context_documents", []) or [])
        file_count = int(getattr(state, "total_files", 0) or 0)

        resources.append(EnvironmentResource(
            id="workspace-root",
            kind="directory",
            label=root,
            location="workspace",
            status="available",
            detail=f"persistent workspace root with {file_count} files",
            metadata={"path": root, "file_count": file_count},
        ))
        for doc in ctx_docs[:12]:
            resources.append(EnvironmentResource(
                id=f"workspace-doc:{doc.path}",
                kind="document",
                label=doc.filename,
                location="workspace",
                status="available",
                detail=f"context document in {doc.path}",
                metadata={"path": doc.path, "mtime": doc.mtime, "size_bytes": doc.size_bytes},
            ))

        if self._tool_registry is not None:
            file_tools = [
                spec.name for spec in self._tool_registry.all_tools()
                if getattr(spec, "pack", "") in {"workspace_tools", "fs_tools"} and getattr(spec, "enabled", False)
            ]
            if file_tools:
                surfaces.append(EnvironmentSurface(
                    id="surface:workspace-tools",
                    kind="toolpack",
                    label="workspace/files tools",
                    location="workspace",
                    status="available",
                    reachability="direct",
                    trust_level="mixed",
                    confirmation_policy="mixed",
                    detail="registered tools that can inspect or modify files in the workspace and filesystem",
                    operations=file_tools[:12],
                    backed_by=["tool_registry"],
                    metadata={"pack_names": ["workspace_tools", "fs_tools"]},
                ))

        return [
            EnvironmentLocation(
                id="workspace",
                label="workspace",
                kind="filesystem",
                status="available",
                summary=f"root={root}; files={file_count}; context_docs={len(ctx_docs)}",
                reachability="direct",
                resources=["workspace-root", *[f"workspace-doc:{doc.path}" for doc in ctx_docs[:12]]],
                surfaces=[s.id for s in surfaces if s.location == "workspace"],
                metadata={"root": root, "context_doc_count": len(ctx_docs)},
            )
        ]

    def _desktop_locations(
        self,
        resources: list[EnvironmentResource],
        surfaces: list[EnvironmentSurface],
    ) -> list[EnvironmentLocation]:
        if self._computer_use_service is None:
            return [
                EnvironmentLocation(
                    id="desktop",
                    label="desktop",
                    kind="computer",
                    status="unavailable",
                    summary="computer-use subsystem not loaded",
                    reachability="none",
                ),
                EnvironmentLocation(
                    id="browser",
                    label="browser",
                    kind="application",
                    status="unknown",
                    summary="browser state unknown because computer-use is unavailable",
                    reachability="indirect",
                ),
            ]

        state = self._computer_use_service.get_state()
        approved = list(state.approved_shortcuts or [])
        active_window = state.active_window_title or ""
        if active_window:
            resources.append(EnvironmentResource(
                id="desktop-active-window",
                kind="window",
                label=active_window,
                location="desktop",
                status="active",
                detail=f"active app={state.active_app_id or 'unknown'}",
                metadata={"app_id": state.active_app_id, "shortcut_id": state.active_shortcut_id},
            ))
        for shortcut in approved[:12]:
            resources.append(EnvironmentResource(
                id=f"shortcut:{shortcut.get('shortcut_id') or shortcut.get('app_id')}",
                kind="application",
                label=shortcut.get("display_name") or shortcut.get("app_id") or "approved app",
                location="desktop",
                status="approved",
                detail=shortcut.get("description", "approved computer-use shortcut"),
                metadata=shortcut,
            ))

        surfaces.append(EnvironmentSurface(
            id="surface:computer-use",
            kind="computer_use",
            label="computer_use",
            location="desktop",
            status=state.mode,
            reachability="direct" if state.mode != "off" else "gated",
            trust_level="verified_user",
            confirmation_policy="per-app policy",
            detail=state.mode_description,
            operations=["open", "read", "type", "save", "navigate"],
            backed_by=["computer_use_service"],
            metadata={
                "approved_shortcuts": len(approved),
                "pending_confirmation": state.pending_confirmation,
                "active_app_id": state.active_app_id,
            },
        ))

        browser_status = "active" if "browser" in (state.active_app_id or "") or "chrome" in active_window.lower() else "available"
        browser_summary = (
            f"active window={active_window}" if active_window else "browser access depends on approved desktop shortcuts"
        )
        return [
            EnvironmentLocation(
                id="desktop",
                label="desktop",
                kind="computer",
                status=state.mode,
                summary=f"mode={state.mode}; approved_apps={len(approved)}; active_app={state.active_app_id or 'none'}",
                reachability="direct" if state.mode != "off" else "gated",
                resources=[res.id for res in resources if res.location == "desktop"],
                surfaces=[s.id for s in surfaces if s.location == "desktop"],
                metadata={"pending_confirmation": state.pending_confirmation},
            ),
            EnvironmentLocation(
                id="browser",
                label="browser",
                kind="application",
                status=browser_status,
                summary=browser_summary,
                reachability="direct" if state.mode != "off" else "indirect",
                surfaces=[s.id for s in surfaces if s.location == "browser"],
                metadata={"active_window_title": active_window},
            ),
        ]

    def _service_locations(
        self,
        resources: list[EnvironmentResource],
        surfaces: list[EnvironmentSurface],
        accounts: list[ConnectedAccount],
    ) -> list[EnvironmentLocation]:
        locations: list[EnvironmentLocation] = []
        capability_map = self._capabilities_by_name()

        google_cfg = self._cfg.get("google", {})
        google_enabled = bool(google_cfg.get("enabled", False))
        google_info = self._google_account_info() if google_enabled else {}
        services_enabled = []
        for svc in ("calendar", "gmail", "drive"):
            if google_cfg.get(f"{svc}_enabled", False):
                services_enabled.append(svc)
        google_status = "disabled"
        google_reachability = "none"
        if google_enabled and services_enabled:
            google_status = "configured"
            google_reachability = "network"
            if google_info.get("authorized"):
                google_status = "connected"
            elif google_info.get("token_exists"):
                google_status = "needs_reauth"
            elif google_info.get("client_secret_exists"):
                google_status = "needs_auth"
            else:
                google_status = "unavailable"

            acct_hint = google_info.get("account", {}).get("email", "")
            accounts.append(ConnectedAccount(
                id="account:google",
                provider="google",
                label="Google Workspace",
                service=",".join(services_enabled),
                status=google_status,
                location="calendar",
                reachable=google_reachability,
                detail=google_info.get("overall_status", google_status),
                identity_hint=acct_hint,
                capabilities=services_enabled,
                metadata=google_info,
            ))

            surfaces.append(EnvironmentSurface(
                id="surface:google-workspace",
                kind="api",
                label="google_workspace_api",
                location="calendar",
                status=google_status,
                reachability=google_reachability,
                trust_level="verified_user",
                confirmation_policy="tool-specific",
                detail="Google Workspace APIs exposed via registered toolpacks when configured",
                operations=[f"google_{svc}" for svc in services_enabled],
                backed_by=["google_oauth", "tool_registry"],
                metadata={"services_enabled": services_enabled},
            ))
        locations.append(EnvironmentLocation(
            id="calendar",
            label="calendar",
            kind="service",
            status=google_status if google_enabled else "disabled",
            summary=(
                f"google calendar/gmail/drive services={','.join(services_enabled) or 'none'}; state={google_status}"
                if google_enabled else
                "external calendar services disabled"
            ),
            reachability=google_reachability,
            surfaces=[s.id for s in surfaces if s.location == "calendar"],
            metadata={"services_enabled": services_enabled},
        ))

        discord_cfg = self._cfg.get("discord", {})
        discord_enabled = bool(discord_cfg.get("enabled", False))
        discord_cap = capability_map.get("discord", {})
        discord_status = "disabled"
        discord_reachability = "none"
        if discord_enabled:
            discord_status = discord_cap.get("status", "configured") or "configured"
            discord_reachability = "network"
            accounts.append(ConnectedAccount(
                id="account:discord",
                provider="discord",
                label="Discord",
                service="messaging",
                status=discord_status,
                location="discord",
                reachable=discord_reachability,
                detail="Discord bot/integration configured in runtime config",
                capabilities=["send_messages", "conversation"],
                metadata={"capability": discord_cap},
            ))
            surfaces.append(EnvironmentSurface(
                id="surface:discord",
                kind="api",
                label="discord_api",
                location="discord",
                status=discord_status,
                reachability=discord_reachability,
                trust_level="verified_user",
                confirmation_policy="tool-specific",
                detail="Discord integration is config-gated and routed through runtime tools/bot",
                operations=["send_discord", "discord_turns"],
                backed_by=["discord_bot", "tool_registry"],
                metadata={"capability": discord_cap},
            ))
        locations.append(EnvironmentLocation(
            id="discord",
            label="discord",
            kind="service",
            status=discord_status,
            summary="Discord messaging surface available" if discord_enabled else "Discord integration disabled",
            reachability=discord_reachability,
            surfaces=[s.id for s in surfaces if s.location == "discord"],
        ))

        browser_tools = self._browser_tools()
        if browser_tools:
            surfaces.append(EnvironmentSurface(
                id="surface:browser-network-tools",
                kind="toolpack",
                label="browser/network tools",
                location="browser",
                status="available",
                reachability="network",
                trust_level="public",
                confirmation_policy="tool-specific",
                detail="web-facing tool surfaces such as web search/fetch/browser helpers",
                operations=browser_tools,
                backed_by=["tool_registry"],
            ))

        runtime_summary = self._runtime_summary()
        surfaces.append(EnvironmentSurface(
            id="surface:runtime-backends",
            kind="backend",
            label="runtime_backends",
            location="runtime",
            status=runtime_summary.get("status", "unknown"),
            reachability="local_process",
            trust_level="system",
            confirmation_policy="none",
            detail=runtime_summary.get("detail", "runtime backend topology and capability state"),
            operations=list(runtime_summary.get("available_roles", [])),
            backed_by=["topology", "capability_registry", "service_discovery"],
            metadata=runtime_summary,
        ))
        locations.append(EnvironmentLocation(
            id="runtime",
            label="runtime",
            kind="backend",
            status=runtime_summary.get("status", "unknown"),
            summary=runtime_summary.get("detail", "runtime status unavailable"),
            reachability="local_process",
            surfaces=[s.id for s in surfaces if s.location == "runtime"],
            metadata=runtime_summary,
        ))
        return locations

    def _append_toolpack_surfaces(self, surfaces: list[EnvironmentSurface]) -> None:
        if self._tool_registry is None:
            return
        pack_map: dict[str, list[Any]] = {}
        for spec in self._tool_registry.all_tools():
            pack_map.setdefault(getattr(spec, "pack", "unknown"), []).append(spec)
        for pack, specs in pack_map.items():
            enabled = [spec for spec in specs if getattr(spec, "enabled", False)]
            if not enabled:
                continue
            trust_levels = sorted({str(getattr(spec, "trust_level", "public")) for spec in enabled})
            confirms = sorted({str(getattr(spec, "confirmation_policy", "none")) for spec in enabled})
            location = self._pack_location(pack)
            surfaces.append(EnvironmentSurface(
                id=f"surface:pack:{pack}",
                kind="toolpack",
                label=pack,
                location=location,
                status="available",
                reachability="direct",
                trust_level=",".join(trust_levels),
                confirmation_policy=",".join(confirms),
                detail=f"{len(enabled)} enabled tool(s) registered in {pack}",
                operations=[spec.name for spec in enabled[:12]],
                backed_by=["tool_registry"],
                metadata={"enabled_count": len(enabled), "total_count": len(specs)},
            ))

    def _pack_location(self, pack: str) -> str:
        if pack in {"workspace_tools", "fs_tools", "ingestion_tools"}:
            return "workspace"
        if pack in {"google_tools", "scheduler_tools"}:
            return "calendar"
        if pack in {"web_tools", "network_tools"}:
            return "browser"
        if pack in {"notifications_tools"}:
            return "discord"
        return "runtime"

    def _browser_tools(self) -> list[str]:
        if self._tool_registry is None:
            return []
        tools: list[str] = []
        for spec in self._tool_registry.all_tools():
            pack = str(getattr(spec, "pack", ""))
            tags = {str(tag) for tag in getattr(spec, "tags", [])}
            if not getattr(spec, "enabled", False):
                continue
            if pack in {"web_tools", "network_tools"} or {"browser", "web", "network"} & tags:
                tools.append(spec.name)
        return tools[:12]

    def _runtime_summary(self) -> dict[str, Any]:
        if self._topology is None:
            return {"status": "unknown", "detail": "runtime topology unavailable", "available_roles": []}
        ready_roles = []
        problem_roles = []
        for role, state in self._topology.servers.items():
            if state.is_ready():
                ready_roles.append(role)
            else:
                problem_roles.append(f"{role}:{state.status.value}")
        status = "healthy" if ready_roles and not problem_roles else "degraded" if ready_roles else "offline"
        detail = f"ready={','.join(ready_roles) or 'none'}"
        if problem_roles:
            detail += f"; limited={','.join(problem_roles)}"
        return {
            "status": status,
            "detail": detail,
            "available_roles": ready_roles,
            "limited_roles": problem_roles,
        }

    def _capabilities_by_name(self) -> dict[str, dict[str, Any]]:
        if self._capability_registry is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for entry in self._capability_registry.all():
            if hasattr(entry, "to_dict"):
                out[entry.name] = entry.to_dict()
        return out

    def _google_account_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        try:
            from core.google_oauth import (
                configure as oauth_configure,
                get_account_info,
                get_credentials,
                is_authorized,
                validate_client_secret_path,
                _token_path,
            )

            oauth_configure(self._cfg)
            creds = get_credentials()
            try:
                validate_client_secret_path()
                client_secret_exists = True
            except FileNotFoundError:
                client_secret_exists = False
            info = {
                "authorized": is_authorized(),
                "account": get_account_info() if creds is not None else {},
                "client_secret_exists": client_secret_exists,
                "token_exists": _token_path().is_file(),
                "overall_status": "connected" if creds is not None else "not_connected",
            }
        except Exception as exc:
            info = {"error": str(exc), "authorized": False, "overall_status": "unavailable"}
        return info

    def _build_summary(
        self,
        locations: list[EnvironmentLocation],
        resources: list[EnvironmentResource],
        surfaces: list[EnvironmentSurface],
        accounts: list[ConnectedAccount],
    ) -> dict[str, Any]:
        available_locations = [loc.label for loc in locations if loc.status not in {"disabled", "unavailable", "unknown"}]
        connected_accounts = [acct.label for acct in accounts if acct.status in {"connected", "configured", "active"}]
        gated_surfaces = [surface.label for surface in surfaces if surface.reachability in {"gated", "indirect"} or "confirm" in surface.confirmation_policy]
        headline = (
            f"Known locations: {', '.join(available_locations) or 'none'}; "
            f"resources={len(resources)}; surfaces={len(surfaces)}; "
            f"connected_services={', '.join(connected_accounts) or 'none'}"
        )
        return {
            "headline": headline,
            "location_count": len(locations),
            "resource_count": len(resources),
            "surface_count": len(surfaces),
            "account_count": len(accounts),
            "available_locations": available_locations,
            "connected_accounts": connected_accounts,
            "gated_surfaces": gated_surfaces,
        }
