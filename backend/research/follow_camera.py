"""Optional explicit handoff; never change scripted camera selection by default."""
from freecam_probe import CAMERA_OBJECT_POINTER, resolve_camera_transform


def handoff_to_follow(client):
    follow = client.read(0x5D9158, 32)
    resolve_camera_transform(client, camera_object=follow)
    if client.read(follow, 32) != 0x535D50:
        raise RuntimeError('Player FollowCamera is unavailable')
    # Use the same paired pointer update as the former standalone application.
    client.write(CAMERA_OBJECT_POINTER, follow, 32)
    client.write(0x5D9150, follow, 32)
    if client.read(CAMERA_OBJECT_POINTER, 32) != follow or client.read(0x5D9150, 32) != follow:
        raise RuntimeError('Player FollowCamera handoff failed')
