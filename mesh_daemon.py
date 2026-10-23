"""Публичный mesh-узел МЫС: ретранслятор для телефонов и десктопов (этап 2).

Отдельный процесс (systemd vsc-mesh): asyncio-ядро из mesh/ (вендорено из
vsc_desktop). Слушает только 127.0.0.1:MESH_PORT кадрами TcpWire; снаружи к нему
ходят через веб-маршрут /mesh (rendezvous_ws.py), который пересылает кадры как есть —
шифрование линка идёт от клиента до этого процесса, веб видит только шифртекст.

  MESH_PORT        порт на 127.0.0.1 (по умолчанию 7811)
  MESH_PUBLIC_URL  свой адрес для PEERS, например wss://soufos.ru/mesh
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesh"))

from mys_mesh.node import MeshConfig, MeshNode  # noqa: E402
from mys_mesh.wire import TcpWire  # noqa: E402

MESH_PORT = int(os.environ.get("MESH_PORT", "7811"))


async def main() -> None:
    node = MeshNode(MeshConfig(relay=True, public_url=os.environ.get("MESH_PUBLIC_URL") or None))

    async def on_link(reader, writer):
        try:
            await node.serve(TcpWire(reader, writer))
        except Exception:
            pass  # битый/чужой линк — просто закрыт

    server = await asyncio.start_server(on_link, "127.0.0.1", MESH_PORT)
    print(f"mesh: 127.0.0.1:{MESH_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
