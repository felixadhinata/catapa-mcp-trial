"""Auto-generates MCP tools from the `catapa` (public) SDK's resource tree.

The `catapa` package exposes ~190 resource namespaces (e.g. `core.employees`,
`timemanagement.attendances`) as an auto-generated, OpenAPI-derived tree
(`catapa.resource_registry.ROOT_RESOURCES`). Each leaf resource wraps an
`openapi_client` API class whose operation methods (`list`, `create`, `retrieve`, ...)
are themselves typed with pydantic models. Rather than hand-writing one tool per
operation (there are several hundred), this module walks that tree at import time
and registers one MCP tool per operation, deriving each tool's JSON schema straight
from the SDK's own type hints.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from catapa import Catapa
from catapa.resource_node import ResourceNode
from catapa.resource_registry import ROOT_RESOURCES

logger = logging.getLogger(__name__)

# openapi-generator emits three variants per operation; only the plain one returns a
# parsed model and is worth exposing as a tool.
_SKIPPED_METHOD_SUFFIXES = ("_with_http_info", "_without_preload_content")

# Kwargs every generated operation method accepts for controlling the HTTP call itself
# (timeout, extra headers, ...) rather than API parameters. These are excluded from the
# tool's input schema; the server always uses the SDK's defaults for them.
_TRANSPORT_PARAM_PREFIX = "_"

TOOL_NAME_PREFIX = "catapa"


def _iter_api_nodes(nodes: Iterable[ResourceNode]) -> Iterator[ResourceNode]:
    """Depth-first walk of the resource tree, yielding every API (leaf or hybrid) node.

    Args:
        nodes: The nodes to walk (pass `ROOT_RESOURCES` for the full tree).

    Yields:
        ResourceNode: Each node that maps to an API class.
    """
    for node in nodes:
        if node.is_api:
            yield node
        yield from _iter_api_nodes(node.children)


def _resolve_resource(client: Catapa, resource_chain: list[str]) -> Any:
    """Walk a resource chain from the client root, e.g. ["core", "employees"].

    Args:
        client: The CATAPA client instance.
        resource_chain: Dotted-path segments from `ResourceNode.resource_chain`.

    Returns:
        Any: The `catapa.resource.Resource` at that path.
    """
    resource: Any = client
    for segment in resource_chain:
        resource = getattr(resource, segment)
    return resource


def _operation_method_names(api_instance: Any) -> list[str]:
    """List the plain (non `_with_http_info` / `_without_preload_content`) operation methods.

    Args:
        api_instance: An `openapi_client.api.*Api` instance.

    Returns:
        list[str]: Sorted operation method names.
    """
    names = []
    for name in dir(api_instance):
        if name.startswith("_") or name.endswith(_SKIPPED_METHOD_SUFFIXES):
            continue
        if callable(getattr(api_instance, name, None)):
            names.append(name)
    return sorted(names)


def _serialize(value: Any) -> Any:
    """Convert an SDK return value (pydantic model, list thereof, or primitive) to plain JSON.

    Args:
        value: The value returned by an SDK operation method.

    Returns:
        Any: A JSON-serializable equivalent.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def _build_tool_function(bound_method: Callable[..., Any], tool_name: str, doc: str) -> Callable[..., Any]:
    """Wrap an SDK operation method as an MCP tool function with a real, matching signature.

    The MCP server derives each tool's JSON schema by inspecting the wrapper function itself,
    not just a `__signature__` override, so the wrapper must be an honest function with the
    SDK method's own parameter names/types/defaults (minus transport-only parameters) --
    a generic `**kwargs` catch-all is not equivalent and produces the wrong schema. The
    wrapper is therefore generated via `exec` from a real function definition, letting the
    SDK's own type hints (including nested pydantic request models) drive the tool schema
    directly, instead of hand-describing several hundred operations.

    Args:
        bound_method: The bound SDK operation method (e.g. `client.core.employees.list`).
        tool_name: The MCP tool name to assign.
        doc: The tool description.

    Returns:
        Callable[..., Any]: An async function suitable for `MCPServer.add_tool`.
    """
    signature = inspect.signature(bound_method)
    params = [
        param
        for name, param in signature.parameters.items()
        if not name.startswith(_TRANSPORT_PARAM_PREFIX)
        and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]

    namespace: dict[str, Any] = {"__bound_method__": bound_method, "__serialize__": _serialize}
    annotations: dict[str, Any] = {}
    arg_defs = []
    call_kwargs = []
    for index, param in enumerate(params):
        annotations[param.name] = param.annotation
        call_kwargs.append(f"{param.name}={param.name}")
        if param.default is inspect.Parameter.empty:
            arg_defs.append(param.name)
        else:
            default_name = f"__default_{index}__"
            namespace[default_name] = param.default
            arg_defs.append(f"{param.name}={default_name}")

    signature_src = f"*, {', '.join(arg_defs)}" if arg_defs else ""
    source = (
        f"async def {tool_name}({signature_src}):\n"
        f"    return __serialize__(__bound_method__({', '.join(call_kwargs)}))\n"
    )
    exec(compile(source, f"<catapa-mcp:{tool_name}>", "exec"), namespace)  # noqa: S102 -- building the tool's own real signature is the point; see docstring

    tool_function = namespace[tool_name]
    tool_function.__annotations__ = annotations
    tool_function.__doc__ = doc
    return tool_function


def register_public_tools(
    server: Any,
    client: Catapa,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> int:
    """Register one MCP tool per operation across the entire `catapa` resource tree.

    Args:
        server: The `MCPServer` to register tools on.
        client: An authenticated `catapa.Catapa` client.
        include: If set, only resource namespaces starting with one of these prefixes
            (e.g. `["core.employees", "timemanagement"]`) are registered.
        exclude: If set, resource namespaces starting with one of these prefixes are skipped.
            Applied after `include`.

    Returns:
        int: The number of tools registered.
    """
    registered = 0
    seen_names: set[str] = set()

    for node in _iter_api_nodes(ROOT_RESOURCES):
        namespace = node.namespace
        if include and not any(namespace.startswith(prefix) for prefix in include):
            continue
        if exclude and any(namespace.startswith(prefix) for prefix in exclude):
            continue

        try:
            resource = _resolve_resource(client, node.resource_chain)
            api_instance = resource._get_api_instance()  # noqa: SLF001 -- Resource exposes no public accessor
        except Exception:
            logger.warning("Skipping unresolvable CATAPA resource %r", namespace, exc_info=True)
            continue

        for operation_name in _operation_method_names(api_instance):
            tool_name = f"{TOOL_NAME_PREFIX}_{namespace.replace('.', '_')}_{operation_name}"
            if tool_name in seen_names:
                logger.warning("Duplicate tool name %r for namespace %r; skipping", tool_name, namespace)
                continue

            bound_method = getattr(api_instance, operation_name)
            description = f"[{namespace}] {operation_name} -- {(node.description or '').strip()}".strip()
            tool_function = _build_tool_function(bound_method, tool_name, description)

            try:
                server.add_tool(tool_function, name=tool_name, description=description)
            except Exception:
                logger.warning("Failed to register tool %r", tool_name, exc_info=True)
                continue

            seen_names.add(tool_name)
            registered += 1

    return registered
