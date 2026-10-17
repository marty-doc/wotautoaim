# -*- coding: utf-8 -*-
"""Optimized target lock and armor aim-point selection for WoT Python 2.7."""
from __future__ import absolute_import

import math
import BigWorld
import Keys
import Math
import game

try:
    import AvatarInputHandler
except ImportError:
    AvatarInputHandler = None

try:
    from shared_utils.vehicle_utils import getMatinfo
    from vehicle_systems.tankStructure import TankPartIndexes
except ImportError:
    getMatinfo = None
    TankPartIndexes = None

try:
    from helpers_common import computePiercingPowerAtDist as _compute_piercing_at_distance
except ImportError:
    _compute_piercing_at_distance = None

# ----------------------------- User settings -------------------------------
TARGET_MAX_DISTANCE = 1500.0
SEARCH_ANGLE_DEGREES = 5.0
TARGET_SEARCH_TIE_TOLERANCE = 0.001
MOUSE_BUTTON = Keys.KEY_RIGHTMOUSE

UPDATE_INTERVAL = 0.05             # 50 ms / 20 Hz, not 15 ms.
LIFE_CHECK_INTERVAL = 0.25

ARMOR_SEARCH_SPEED_THRESHOLD_KMH = 10.0
ARMOR_SEARCH_GRID_LOW_SPEED = 7
ARMOR_SEARCH_GRID_HIGH_SPEED = 3
ARMOR_SEARCH_INTERVAL_LOW_SPEED = 0.10
ARMOR_SEARCH_INTERVAL_HIGH_SPEED = 0.10
ARMOR_SEARCH_GRID_SIZE = ARMOR_SEARCH_GRID_LOW_SPEED
ARMOR_SEARCH_INTERVAL = ARMOR_SEARCH_INTERVAL_LOW_SPEED

# Expensive operations are throttled separately from the update loop.
ARMOR_POINT_REVALIDATE_INTERVAL = 0.10
ARMOR_CANDIDATE_SEARCH_INTERVAL = 0.35
ARMOR_POINT_EPSILON = 0.20
ARMOR_MAX_IMPACT_ANGLE_DEGREES = 45.0
ARMOR_MIN_HIT_COS = math.cos(math.radians(ARMOR_MAX_IMPACT_ANGLE_DEGREES))

MIN_VECTOR_LENGTH = 0.000001
OWN_TANK_MOVING_THRESHOLD_KMH = 0.5
WORLD_COLLISION_MASK = 128

# ------------------------------ State --------------------------------------
_target = None
_target_id = None
_target_point = None
_target_hull_point = None
_target_turret_point = None
_target_turret_enable = False
_our_vehicle_moving = False
_our_vehicle_speed_kmh = 0.0
_target_last_armor_search = -999.0
_target_last_revalidate = -999.0
_target_point_source_mode = None
_target_expanded_point = None
_target_expanded_part = None
_target_tracking_cell = None
_target_tracking_part = None
_input_class_hooked = False
_original_input_handle_key_event = None
_rmb_was_down = False
_rmb_event_seen = False
_rotator = None
_original_shot_point_source = None
_custom_shot_point_source = None
_target_last_life_check = -999.0


def _get_player():
    try:
        return BigWorld.player()
    except Exception:
        return None


def _is_on_arena(player):
    try:
        return bool(player.isOnArena)
    except Exception:
        return False


def _vehicle_position(vehicle):
    if vehicle is None:
        return None
    try:
        return Math.Vector3(vehicle.position)
    except Exception:
        try:
            return Math.Vector3(vehicle.matrix.translation)
        except Exception:
            return None


def _is_vehicle_alive(vehicle):
    if vehicle is None:
        return False
    try:
        state = getattr(vehicle, 'isAlive', None)
        if callable(state):
            return bool(state())
        if state is not None:
            return bool(state)
    except Exception:
        return False
    return False


def _is_own_tank_alive(player=None):
    if player is None:
        player = _get_player()
    if player is None or not _is_on_arena(player):
        return False
    try:
        return _is_vehicle_alive(player.getVehicleAttached())
    except Exception:
        return False


def _is_target_alive(target=None, target_id=None):
    if target is None:
        target = _target
    if target_id is None and target is not None:
        target_id = getattr(target, 'id', None)
    if target is None or target_id is None:
        return False
    player = _get_player()
    if player is None or not _is_on_arena(player):
        return False
    try:
        info = player.arena.vehicles.get(target_id)
        if (info is None or not info.get('isAlive', False) or
                info.get('team') == getattr(player, 'team', None)):
            return False
    except Exception:
        return False
    try:
        entity = BigWorld.entity(target_id)
    except Exception:
        entity = target
    return _is_vehicle_alive(entity)


def _camera_center_ray():
    try:
        import AvatarInputHandler.cameras as cameras
        return cameras.getWorldRayAndPosition()
    except Exception:
        pass
    try:
        projection = BigWorld.projection()
        inv_view = Math.Matrix(BigWorld.camera().invViewMatrix)
        ray = inv_view.applyVector(
            Math.Vector3(0.0, 0.0, projection.nearPlane))
        origin = inv_view.applyPoint(Math.Vector3(0.0, 0.0, 0.0))
        return ray, origin
    except Exception:
        return None, None


def _hull_center(vehicle):
    try:
        descriptor = vehicle.typeDescriptor
        bbox_min, bbox_max, unused = descriptor.hull.hitTester.bbox
        center = (Math.Vector3(bbox_min) + Math.Vector3(bbox_max)) * 0.5
        center += Math.Vector3(descriptor.chassis.hullPosition)
        return Math.Matrix(vehicle.matrix).applyPoint(center)
    except Exception:
        return _vehicle_position(vehicle)


def _turret_center(vehicle):
    try:
        descriptor = vehicle.typeDescriptor
        positions = descriptor.hull.turretPositions
        if not positions:
            return None
        bbox_min, bbox_max, unused = descriptor.turret.hitTester.bbox
        center = (Math.Vector3(bbox_min) + Math.Vector3(bbox_max)) * 0.5
        center += Math.Vector3(positions[0])
        center += Math.Vector3(descriptor.chassis.hullPosition)
        return Math.Matrix(vehicle.matrix).applyPoint(center)
    except Exception:
        return None


def _refresh_target_coordinates(force_turret_probe=True):
    """Refresh centers only; never overwrite the active armor aim point."""
    global _target_hull_point, _target_turret_point, _target_turret_enable
    if _target is None:
        _target_hull_point = None
        _target_turret_point = None
        _target_turret_enable = False
        return False
    _target_hull_point = _hull_center(_target)
    turret = _turret_center(_target) if force_turret_probe else _target_turret_point
    _target_turret_point = turret
    _target_turret_enable = turret is not None
    return _target_hull_point is not None or turret is not None


def _get_gun_rotator(player):
    for getter in (
            lambda: player.gunRotator,
            lambda: player.inputHandler.gunRotator,
            lambda: player.inputHandler.ctrl.gunRotator):
        try:
            rotator = getter()
            if rotator is not None:
                return rotator
        except Exception:
            pass
    return None


def _restore_shot_point_source():
    global _rotator, _original_shot_point_source, _custom_shot_point_source
    if _rotator is not None:
        try:
            if _rotator.shotPointSourceFunctor is _custom_shot_point_source:
                _rotator.shotPointSourceFunctor = _original_shot_point_source
        except Exception:
            pass
    _rotator = None
    _original_shot_point_source = None
    _custom_shot_point_source = None


def _install_shot_point_source():
    global _rotator, _original_shot_point_source, _custom_shot_point_source
    player = _get_player()
    if player is None or _target is None or _target_point is None:
        return False
    rotator = _get_gun_rotator(player)
    if rotator is None:
        return False
    if rotator is _rotator and _custom_shot_point_source is not None:
        try:
            if rotator.shotPointSourceFunctor is _custom_shot_point_source:
                return True
            rotator.shotPointSourceFunctor = _custom_shot_point_source
            return True
        except Exception:
            return False
    _restore_shot_point_source()
    try:
        original = rotator.shotPointSourceFunctor
    except Exception:
        original = None
    _rotator = rotator
    _original_shot_point_source = original

    def target_point_source():
        if _target is not None and _target_point is not None:
            return _target_point
        if _original_shot_point_source is not None:
            try:
                return _original_shot_point_source()
            except Exception:
                pass
        return None

    _custom_shot_point_source = target_point_source
    try:
        rotator.shotPointSourceFunctor = _custom_shot_point_source
        return rotator.shotPointSourceFunctor is _custom_shot_point_source
    except Exception:
        _rotator = None
        _original_shot_point_source = None
        _custom_shot_point_source = None
        return False


def _get_own_vehicle_speed_kmh(player):
    try:
        vehicle = player.getVehicleAttached()
        return float(Math.Vector3(vehicle.velocity).length) * 3.6
    except Exception:
        return 0.0


def _update_our_vehicle_moving(player):
    global _our_vehicle_moving, _our_vehicle_speed_kmh
    global ARMOR_SEARCH_INTERVAL, ARMOR_SEARCH_GRID_SIZE
    speed = max(0.0, _get_own_vehicle_speed_kmh(player))
    _our_vehicle_speed_kmh = speed
    if speed <= ARMOR_SEARCH_SPEED_THRESHOLD_KMH:
        ARMOR_SEARCH_INTERVAL = ARMOR_SEARCH_INTERVAL_LOW_SPEED
        ARMOR_SEARCH_GRID_SIZE = ARMOR_SEARCH_GRID_LOW_SPEED
    else:
        ARMOR_SEARCH_INTERVAL = ARMOR_SEARCH_INTERVAL_HIGH_SPEED
        ARMOR_SEARCH_GRID_SIZE = ARMOR_SEARCH_GRID_HIGH_SPEED
    _our_vehicle_moving = speed > OWN_TANK_MOVING_THRESHOLD_KMH
    return speed


def _get_vehicle_descriptor(player):
    try:
        return player.getVehicleDescriptor()
    except Exception:
        return getattr(player, 'vehicleTypeDescriptor', None)


def _get_muzzle_world_position(player):
    descriptor = _get_vehicle_descriptor(player)
    if descriptor is None:
        return None
    try:
        turret_position = descriptor.hull.turretPositions[0]
        hull_position = descriptor.chassis.hullPosition
        gun_offset = descriptor.activeGunShotPosition
        vehicle_matrix = Math.Matrix(player.getOwnVehicleStabilisedMatrix())
    except Exception:
        return None
    turret_yaw = 0.0
    try:
        rotator = _get_gun_rotator(player)
        if rotator is not None:
            turret_yaw = float(rotator.turretYaw)
    except Exception:
        pass
    try:
        turret_matrix = Math.Matrix()
        turret_matrix.setRotateY(turret_yaw)
        turret_matrix.translation = turret_position + hull_position
        turret_matrix.postMultiply(vehicle_matrix)
        return turret_matrix.applyPoint(gun_offset)
    except Exception:
        return None


def _part_bbox_and_offset(vehicle, part):
    try:
        descriptor = vehicle.typeDescriptor
        if part == 'TURRET':
            positions = descriptor.hull.turretPositions
            if not positions:
                return None, None
            bbox = descriptor.turret.hitTester.bbox
            offset = (Math.Vector3(descriptor.chassis.hullPosition) +
                      Math.Vector3(positions[0]))
        else:
            bbox = descriptor.hull.hitTester.bbox
            offset = Math.Vector3(descriptor.chassis.hullPosition)
        return bbox, offset
    except Exception:
        return None, None


def _collision_is_part(vehicle, part_id, part):
    if TankPartIndexes is None:
        return True
    try:
        if part_id == TankPartIndexes.HULL:
            parent = TankPartIndexes.HULL
        elif part_id == TankPartIndexes.TURRET:
            parent = TankPartIndexes.TURRET
        else:
            parent = vehicle.appearance.collisions.getParentPartIndex(part_id)
        wanted = (TankPartIndexes.TURRET if part == 'TURRET'
                  else TankPartIndexes.HULL)
        return parent == wanted
    except Exception:
        return False


def _candidate_path_visible(player, target, start, end):
    try:
        space_id = target.spaceID
    except Exception:
        space_id = getattr(player, 'spaceID', None)
    if space_id is None:
        return True
    try:
        collision = BigWorld.wg_collideSegment(
            space_id, start, end, WORLD_COLLISION_MASK)
    except Exception:
        collision = None
    if collision is not None:
        try:
            if (collision.closestPoint - end).length > ARMOR_POINT_EPSILON:
                return False
        except Exception:
            pass
    own_id = -1
    try:
        own_vehicle = player.getVehicleAttached()
        own_id = int(getattr(own_vehicle, 'id', -1))
        collision = BigWorld.wg_collideDynamic(space_id, start, end, own_id)
    except Exception:
        collision = None
    if collision is not None:
        try:
            entity_id = int(getattr(collision.entity, 'id', -1))
            if entity_id not in (own_id, getattr(target, 'id', -2)):
                return False
        except Exception:
            pass
    return True


def _first_visible_penetrable_point(player, vehicle, part, muzzle,
                                    grid_size=None, preferred_cell=None,
                                    return_cell=False, preferred_only=False):
    """Return valid point nearest this part's center; preferred-only is cheap."""
    if getMatinfo is None or muzzle is None:
        return (None, None) if return_cell else None
    bbox, offset = _part_bbox_and_offset(vehicle, part)
    if bbox is None or offset is None:
        return (None, None) if return_cell else None
    try:
        bbox_min, bbox_max = Math.Vector3(bbox[0]), Math.Vector3(bbox[1])
        span_x = bbox_max.x - bbox_min.x
        span_y = bbox_max.y - bbox_min.y
        span_z = bbox_max.z - bbox_min.z
        matrix = Math.Matrix(vehicle.matrix)
        collisions = vehicle.appearance.collisions
        if collisions is None:
            return (None, None) if return_cell else None
        center = matrix.applyPoint((bbox_min + bbox_max) * 0.5 + offset)
    except Exception:
        return (None, None) if return_cell else None

    if grid_size is None:
        grid_size = ARMOR_SEARCH_GRID_SIZE
    grid = max(2, int(grid_size))
    fractions = [float(i) / float(grid - 1) for i in xrange(grid)]
    cells = [(ix, iy, iz) for ix in xrange(grid)
             for iy in xrange(grid) for iz in xrange(grid)]
    if preferred_only:
        if preferred_cell is None or preferred_cell not in cells:
            return (None, None) if return_cell else None
        cells = [preferred_cell]
    elif preferred_cell in cells:
        cells.remove(preferred_cell)
        cells.insert(0, preferred_cell)

    wheeled = False
    try:
        wheeled = bool(vehicle.typeDescriptor.type.isWheeledVehicle)
    except Exception:
        pass
    try:
        descriptor = _get_vehicle_descriptor(player)
        profile = descriptor.shot.piercingPower
        penetration = float(profile[0])
    except Exception:
        return (None, None) if return_cell else None

    best_point = None
    best_cell = None
    best_distance = None
    for ix, iy, iz in cells:
        local = Math.Vector3(
            bbox_min.x + span_x * fractions[ix],
            bbox_min.y + span_y * fractions[iy],
            bbox_min.z + span_z * fractions[iz]) + offset
        try:
            world = matrix.applyPoint(local)
            delta = world - muzzle
            length = delta.length
            if length <= MIN_VECTOR_LENGTH:
                continue
            direction = delta / length
            hits = collisions.collideAllWorld(muzzle, world)
        except Exception:
            continue
        if not hits:
            continue
        for dist, angle_cos, mat_kind, part_id in hits:
            if not _collision_is_part(vehicle, part_id, part):
                continue
            try:
                if abs(float(angle_cos)) < ARMOR_MIN_HIT_COS:
                    continue
                mat_info = getMatinfo(vehicle, part_id, mat_kind, wheeled)
                armor = float(mat_info.armor)
                hit_point = muzzle + direction * float(dist)
            except Exception:
                continue
            if penetration < armor:
                continue
            if not _candidate_path_visible(player, vehicle, muzzle, hit_point):
                continue
            if preferred_only:
                return (hit_point, (ix, iy, iz)) if return_cell else hit_point
            try:
                distance = (hit_point - center).length
            except Exception:
                continue
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_point = hit_point
                best_cell = (ix, iy, iz)

    return (best_point, best_cell) if return_cell else best_point


def _is_candidate_closer_to_center(center, current_point, candidate_point):
    if center is None or current_point is None or candidate_point is None:
        return False
    try:
        center = Math.Vector3(center)
        current_distance = (Math.Vector3(current_point) - center).length
        candidate_distance = (Math.Vector3(candidate_point) - center).length
        return candidate_distance < current_distance
    except Exception:
        return False


def _should_switch_tracking_point(current_part, current_point,
                                  candidate_part, candidate_point,
                                  hull_center, turret_center):
    if candidate_point is None or candidate_part is None:
        return False
    if current_point is None or current_part is None:
        return True
    if current_part == 'TURRET' and candidate_part == 'HULL':
        return True
    if current_part == 'HULL' and candidate_part == 'TURRET':
        return False
    if current_part == candidate_part:
        center = hull_center if candidate_part == 'HULL' else turret_center
        return _is_candidate_closer_to_center(
            center, current_point, candidate_point)
    return False


def _find_expanded_aim_point(player, vehicle, grid_size=None):
    """Scan all hull cells first; scan turret only if hull has no valid point."""
    global _target_expanded_part, _target_tracking_cell
    _target_expanded_part = None
    _target_tracking_cell = None
    muzzle = _get_muzzle_world_position(player)
    if muzzle is None:
        return None
    point, cell = _first_visible_penetrable_point(
        player, vehicle, 'HULL', muzzle, grid_size, None, True, False)
    if point is not None:
        _target_expanded_part = 'HULL'
        _target_tracking_cell = cell
        return point
    if _target_turret_enable and _target_turret_point is not None:
        point, cell = _first_visible_penetrable_point(
            player, vehicle, 'TURRET', muzzle, grid_size, None, True, False)
        if point is not None:
            _target_expanded_part = 'TURRET'
            _target_tracking_cell = cell
            return point
    return None


def _choose_tracking_point(player):
    """Validate active cell periodically and compare periodic full-scan candidate."""
    global _target_point_source_mode, _target_point
    global _target_expanded_point, _target_expanded_part
    global _target_tracking_cell, _target_tracking_part
    global _target_last_armor_search, _target_last_revalidate

    if _target is None:
        _target_point = None
        _target_point_source_mode = None
        _target_expanded_point = None
        _target_expanded_part = None
        _target_tracking_cell = None
        _target_tracking_part = None
        return None

    hull_center = _target_hull_point
    turret_center = _target_turret_point
    if hull_center is None:
        _target_point = None
        _target_point_source_mode = None
        _target_tracking_cell = None
        _target_tracking_part = None
        return None

    now = BigWorld.time()
    active = (_target_point is not None and
              _target_tracking_part in ('HULL', 'TURRET') and
              _target_tracking_cell is not None)

    if active and now - _target_last_revalidate >= ARMOR_POINT_REVALIDATE_INTERVAL:
        _target_last_revalidate = now
        muzzle = _get_muzzle_world_position(player)
        if muzzle is not None and (
                _target_tracking_part == 'HULL' or _target_turret_enable):
            point, cell = _first_visible_penetrable_point(
                player, _target, _target_tracking_part, muzzle,
                ARMOR_SEARCH_GRID_SIZE, _target_tracking_cell, True, True)
            if point is not None:
                _target_point = point
                _target_tracking_cell = cell
            else:
                _target_point = None
                _target_tracking_cell = None
                _target_tracking_part = None
                active = False

    search_due = (not active or _target_point is None or
                  now - _target_last_armor_search >=
                  ARMOR_CANDIDATE_SEARCH_INTERVAL)
    if search_due:
        _target_last_armor_search = now
        # A full scan uses shared scratch state for its candidate cell.
        # Preserve the active tracking cell unless the candidate is accepted.
        current_cell = _target_tracking_cell
        candidate = _find_expanded_aim_point(
            player, _target, ARMOR_SEARCH_GRID_SIZE)
        candidate_part = _target_expanded_part
        candidate_cell = _target_tracking_cell
        if candidate is not None and _should_switch_tracking_point(
                _target_tracking_part, _target_point,
                candidate_part, candidate,
                hull_center, turret_center):
            _target_point = candidate
            _target_tracking_part = candidate_part
            _target_tracking_cell = candidate_cell
        else:
            _target_tracking_cell = current_cell

    if _target_point is not None and _target_tracking_part is not None:
        _target_point_source_mode = (
            'hull-armor-point' if _target_tracking_part == 'HULL'
            else 'turret-armor-point')
        return _target_point

    _target_point = hull_center
    _target_point_source_mode = 'hull-center'
    _target_tracking_cell = None
    _target_tracking_part = None
    return hull_center


def _find_target():
    player = _get_player()
    if player is None or not _is_on_arena(player) or not _is_own_tank_alive(player):
        return None, None
    team = getattr(player, 'team', None)
    if team is None:
        return None, None
    try:
        own_vehicle = player.getVehicleAttached()
        if own_vehicle is None or not _is_vehicle_alive(own_vehicle):
            return None, None
        own_position = _vehicle_position(own_vehicle)
        ray, ray_origin = _camera_center_ray()
        if own_position is None or ray is None or ray_origin is None:
            return None, None
        ray_length = ray.length
        if ray_length <= MIN_VECTOR_LENGTH:
            return None, None
        ray = ray / ray_length
        vehicles = player.arena.vehicles
    except Exception:
        return None, None

    player_id = getattr(player, 'id', None)
    best_vehicle = None
    best_id = None
    best_angle = SEARCH_ANGLE_DEGREES + 1.0
    best_distance = TARGET_MAX_DISTANCE
    for vehicle_id in vehicles.keys():
        if vehicle_id == player_id:
            continue
        try:
            info = vehicles[vehicle_id]
            if not info.get('isAlive', False) or info.get('team') == team:
                continue
            vehicle = BigWorld.entity(vehicle_id)
            if not _is_vehicle_alive(vehicle):
                continue
            position = _vehicle_position(vehicle)
            if position is None:
                continue
            distance = (position - own_position).length
            if distance > TARGET_MAX_DISTANCE:
                continue
            direction = position - ray_origin
            direction_length = direction.length
            if direction_length <= MIN_VECTOR_LENGTH:
                angle = 0.0
            else:
                direction = direction / direction_length
                cosine = max(-1.0, min(1.0, ray.dot(direction)))
                angle = math.degrees(math.acos(cosine))
            if angle > SEARCH_ANGLE_DEGREES:
                continue
            if (best_vehicle is None or
                    angle < best_angle - TARGET_SEARCH_TIE_TOLERANCE or
                    (abs(angle - best_angle) <= TARGET_SEARCH_TIE_TOLERANCE and
                     distance < best_distance)):
                best_vehicle, best_id = vehicle, vehicle_id
                best_angle, best_distance = angle, distance
        except Exception:
            continue
    return best_vehicle, best_id


def _clear_target():
    global _target, _target_id, _target_last_life_check
    global _target_last_armor_search, _target_last_revalidate
    global _target_point, _target_point_source_mode, _target_expanded_point
    global _target_expanded_part, _target_tracking_cell, _target_tracking_part
    _target = None
    _target_id = None
    _target_point = None
    _target_last_life_check = -999.0
    _target_last_armor_search = -999.0
    _target_last_revalidate = -999.0
    _target_point_source_mode = None
    _target_expanded_point = None
    _target_expanded_part = None
    _target_tracking_cell = None
    _target_tracking_part = None
    _refresh_target_coordinates()
    _restore_shot_point_source()


def _set_target(vehicle, vehicle_id):
    global _target, _target_id, _target_last_life_check
    global _target_last_armor_search, _target_last_revalidate
    global _target_point, _target_point_source_mode, _target_expanded_point
    global _target_expanded_part, _target_tracking_cell, _target_tracking_part
    if vehicle is None or vehicle_id is None:
        _clear_target()
        return False
    _target = vehicle
    _target_id = vehicle_id
    _target_point = None
    _target_last_life_check = -999.0
    _target_last_armor_search = -999.0
    _target_last_revalidate = -999.0
    _target_point_source_mode = None
    _target_expanded_point = None
    _target_expanded_part = None
    _target_tracking_cell = None
    _target_tracking_part = None
    if not _refresh_target_coordinates():
        _clear_target()
        return False
    _target_point = _target_hull_point
    if _target_point is not None:
        _install_shot_point_source()
    return True


def _acquire_target():
    vehicle, vehicle_id = _find_target()
    return _set_target(vehicle, vehicle_id) if vehicle is not None else False


def _process_rmb(is_down, key, is_repeat):
    global _rmb_event_seen
    if is_repeat or not is_down or key != MOUSE_BUTTON:
        return False
    _rmb_event_seen = True
    if _target is not None:
        _clear_target()
    else:
        _acquire_target()
    return True


def _poll_rmb():
    global _rmb_was_down, _rmb_event_seen
    try:
        is_down = bool(BigWorld.isKeyDown(MOUSE_BUTTON))
    except Exception:
        is_down = False
    pressed = is_down and not _rmb_was_down
    _rmb_was_down = is_down
    if not is_down:
        _rmb_event_seen = False
        return
    if _rmb_event_seen or not pressed:
        return
    if _target is not None:
        _clear_target()
    else:
        _acquire_target()


def _install_class_input_hook():
    global _input_class_hooked, _original_input_handle_key_event
    if _input_class_hooked or AvatarInputHandler is None:
        return _input_class_hooked
    try:
        input_class = AvatarInputHandler.AvatarInputHandler
        original = input_class.handleKeyEvent
    except Exception:
        return False
    _original_input_handle_key_event = original

    def hooked_handle_key_event(self, event):
        try:
            is_down, key, mods, is_repeat = game.convertKeyEvent(event)
            if _process_rmb(is_down, key, is_repeat):
                return True
        except Exception:
            pass
        return _original_input_handle_key_event(self, event)

    try:
        input_class.handleKeyEvent = hooked_handle_key_event
        _input_class_hooked = True
        return True
    except Exception:
        _original_input_handle_key_event = None
        return False


def _restore_class_input_hook():
    global _input_class_hooked, _original_input_handle_key_event
    if not _input_class_hooked:
        return
    try:
        if AvatarInputHandler is not None and _original_input_handle_key_event is not None:
            AvatarInputHandler.AvatarInputHandler.handleKeyEvent = (
                _original_input_handle_key_event)
    except Exception:
        pass
    _input_class_hooked = False
    _original_input_handle_key_event = None


def _update():
    global _target, _target_last_life_check, _target_point
    player = _get_player()
    if player is None or not _is_on_arena(player):
        if _target is not None:
            _clear_target()
        _restore_class_input_hook()
        _restore_shot_point_source()
        BigWorld.callback(UPDATE_INTERVAL, _update)
        return

    _install_class_input_hook()
    _poll_rmb()
    _update_our_vehicle_moving(player)

    if _target is not None:
        now = BigWorld.time()
        if now - _target_last_life_check >= LIFE_CHECK_INTERVAL:
            _target_last_life_check = now
            if not _is_own_tank_alive(player) or not _is_target_alive(_target, _target_id):
                _clear_target()

        if _target is not None:
            try:
                current = BigWorld.entity(_target_id)
                if current is not None and current is not _target:
                    _target = current
            except Exception:
                pass
            if _target is not None and _refresh_target_coordinates():
                point = _choose_tracking_point(player)
                if point is not None:
                    _target_point = point
                    _install_shot_point_source()
                else:
                    _target_point = None
            elif _target is not None:
                _clear_target()

    BigWorld.callback(UPDATE_INTERVAL, _update)


def get_target():
    return _target


def get_target_id():
    return _target_id


def get_target_point():
    return _target_point


def get_target_hull_point():
    return _target_hull_point


def get_target_turret_point():
    return _target_turret_point


def is_our_vehicle_moving():
    return _our_vehicle_moving


def is_target_turret_available():
    return _target_turret_enable


def has_target():
    return _target is not None


def start():
    _clear_target()
    _install_class_input_hook()
    BigWorld.callback(UPDATE_INTERVAL, _update)


start()
