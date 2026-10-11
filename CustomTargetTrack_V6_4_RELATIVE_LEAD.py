# -*- coding: utf-8 -*-
"""
Custom target acquisition, adaptive armor tracking and automatic fire mod.

Version: V6.4

Python 2.7 / World of Tanks client.

The mod is deliberately independent of ArcadeControlMode and
SniperControlMode.  It hooks AvatarInputHandler.handleKeyEvent at the
common class level, so RMB is processed before the current camera/control
mode gets a chance to consume it.

Behavior:
    * Camera remains completely free.
    * RMB acquires the nearest live enemy inside a cone around the
      camera center ray and within TARGET_MAX_DISTANCE.
    * The selected target is stored globally.
    * VehicleGunRotator receives the selected world-space point through
      its common shotPointSourceFunctor.
    * RMB again clears the target and restores normal gun-point handling.
    * Armor selection adapts to speed-state and target-aspect changes with
      hysteresis, while preserving a stable local-space lock.
    * Candidate ranking can prefer points that the current shell can actually
      penetrate at the current distance.
    * Stationary targets use effective-armor optimization; moving targets use
      stable hull-first point selection with radial/lateral lead.
    * No chat or log output is produced.
"""

from __future__ import absolute_import

import math

import BigWorld
import Keys
import Math
import game

try:
    from shared_utils.vehicle_utils import getMatinfo
    from vehicle_systems.tankStructure import TankPartIndexes
except ImportError:
    getMatinfo = None
    TankPartIndexes = None

try:
    import AvatarInputHandler
    import AvatarInputHandler.cameras as cameras
except ImportError:
    AvatarInputHandler = None
    cameras = None


# ---------------------------------------------------------------------------
# Configuration — все параметры мода находятся здесь
# ---------------------------------------------------------------------------

# Максимальная дальность поиска/сопровождения цели, метры.
TARGET_MAX_DISTANCE = 700.0

# Полуугол конуса поиска от центрального луча камеры, градусы.
SEARCH_ANGLE_DEGREES = 3.0

# Период основного цикла мода, секунды.
UPDATE_INTERVAL = 0.05

# Часть машины, используемая как базовая точка цели.
TARGET_PART = 'HULL'

# Кнопка захвата/сброса цели.
MOUSE_BUTTON = Keys.KEY_RIGHTMOUSE

# Если True, цель сбрасывается после выхода за TARGET_MAX_DISTANCE.
REQUIRE_DISTANCE_WHILE_TRACKING = True

# ---------------------------------------------------------------------------
# Скоростной разделитель «стоящая / движущаяся цель»
# ---------------------------------------------------------------------------

# Порог скорости в км/ч. Цель и наша машина считаются стоящими, если
# их текущая скорость НЕ превышает это значение.
STATIONARY_SPEED_KMH = 10.0

# Тот же порог в м/с — внутренняя единица BigWorld.
STATIONARY_SPEED_MPS = STATIONARY_SPEED_KMH / 3.6

# ---------------------------------------------------------------------------
# Выбор бронезоны
# ---------------------------------------------------------------------------

# Включить поиск точки попадания по геометрии брони.
ARMOR_ZONE_ENABLED = True

# Размер сетки выборки по каждой координате bbox: 3 означает до 27 точек.
ARMOR_ZONE_GRID_SIZE = 3

# Совместимость со старыми конфигами; текущий селектор оценивает все кандидаты.
ARMOR_ZONE_TOP_CANDIDATES = 3

# Как часто разрешено заново искать броневую точку, секунды.
ARMOR_ZONE_UPDATE_INTERVAL = 0.15

# Интервал мягкой перепроверки: при движении/повороте цели разрешаем
# искать более выгодную точку, но не чаще этого значения.
ARMOR_ZONE_RESELECT_INTERVAL = 0.75

# Минимальное улучшение эффективной брони новой точки относительно текущей,
# чтобы менять уже удерживаемую точку. 10% = новая точка должна быть минимум
# на 10% выгоднее; это предотвращает дрожание между близкими кандидатами.
ARMOR_ZONE_RESELECT_IMPROVEMENT_PERCENT = 10.0

# При изменении ракурса цели сильнее этого угла разрешаем немедленную
# адаптивную переоценку бронезоны. Малые изменения игнорируются.
ARMOR_ZONE_ASPECT_RESELECT_DEGREES = 8.0

# Учитывать реальную пробиваемость текущего снаряда при выборе точки.
# Если хотя бы одна кандидатная точка пробивается, предпочтение получают
# пробиваемые точки; среди них сохраняется выбор по effective armor.
ARMOR_ZONE_PENETRATION_AWARE = True
ARMOR_ZONE_PENETRATION_MARGIN = 0.0

# Частота перепроверки видимости сохранённой armor-точки. Между проверками
# точка только перепроецируется из локальных координат цели без collision-запросов.
ARMOR_ZONE_VISIBILITY_CHECK_INTERVAL = 0.10

# Минимальный |cos| угла попадания, чтобы не принимать почти касательное попадание.
ARMOR_ZONE_MIN_HIT_COS = 0.05

# Допуск, на котором точка считается совпавшей с поверхностью цели, метры.
ARMOR_ZONE_POINT_EPSILON = 0.20

# Эталонный угол входа. 90° означает попадание максимально перпендикулярно
# плоскости брони; чем ближе фактический угол к этому значению, тем лучше.
ARMOR_ZONE_TARGET_IMPACT_ANGLE_DEGREES = 90.0

# Доля эффективной брони, внутри которой считаем две точки «близкими» и
# переходим к вторичным критериям: тоньше броня -> угол ближе к эталону.
ARMOR_ZONE_EFFECTIVE_ARMOR_TIE_PERCENT = 0.05

# ---------------------------------------------------------------------------
# Баллистика
# ---------------------------------------------------------------------------

# Включить самостоятельный расчёт поправки по скорости снаряда и гравитации.
BALLISTIC_ENABLED = True

# Минимальная допустимая скорость снаряда, м/с.
BALLISTIC_MIN_SPEED = 1.0

# Минимальная допустимая гравитация снаряда.
BALLISTIC_MIN_GRAVITY = 0.000001

# Минимальная горизонтальная дальность для баллистического расчёта, м.
BALLISTIC_MIN_RANGE = 0.01

# Максимальная вертикальная баллистическая коррекция точки, м.
BALLISTIC_MAX_CORRECTION = 50.0

# Использовать projectileSpeedFactor из commonConfig клиента.
BALLISTIC_USE_SPEED_FACTOR = True

# Допуск проверки рассчитанной траектории, м.
BALLISTIC_VALIDATION_TOLERANCE = 0.05

# ---------------------------------------------------------------------------
# Упреждение движущейся цели
# ---------------------------------------------------------------------------

# Включить упреждение по скорости цели.
BALLISTIC_LEAD_ENABLED = True

# Число итераций уточнения времени полёта снаряда.
BALLISTIC_LEAD_ITERATIONS = 8

# Максимальное время полёта, используемое для упреждения, секунды.
BALLISTIC_LEAD_MAX_TIME = 8.0

# Минимальное время полёта, используемое для упреждения, секунды.
BALLISTIC_LEAD_MIN_TIME = 0.0

# Порог сходимости итераций по времени, секунды.
BALLISTIC_LEAD_EPSILON = 0.001

# Коэффициент дополнительного радиального упреждения. 1.0 = физически нейтрально.
BALLISTIC_LEAD_RADIAL_FACTOR = 1.00

# Коэффициент дополнительного бокового упреждения. 1.0 = физически нейтрально.
BALLISTIC_LEAD_LATERAL_FACTOR = 1.35

# ---------------------------------------------------------------------------
# Автоматический огонь
# ---------------------------------------------------------------------------

# Главный переключатель автоматического огня.
AUTO_FIRE_ENABLED = True

# Автоматический огонь разрешён только когда цель по порогу скорости считается стоящей.
AUTO_FIRE_TARGET_STATIONARY_ONLY = True

# Автоматический огонь разрешён только когда наша машина тоже считается стоящей.
AUTO_FIRE_OWN_STATIONARY_ONLY = True

# Допуск к полному сведению в процентах.
# Например, 10.0 означает: текущее значение dispersionAngle должно
# находиться в диапазоне от 90% до 110% базового значения полного сведения.
AUTO_FIRE_FULL_AIM_TOLERANCE_PERCENT = 10.0

# Минимальный интервал между попытками автоматического выстрела, секунды.
AUTO_FIRE_RETRY_INTERVAL = 0.10

# Проверять внутренний canShoot текущего control mode перед выстрелом.
AUTO_FIRE_REQUIRE_ENGINE_CAN_SHOOT = False

# Перед выстрелом ещё раз проверять, что фактическая точка не перекрыта
# статической геометрией карты или другой динамической сущностью.
AUTO_FIRE_REQUIRE_CLEAR_PATH = True

# Проверять бронепробитие выбранной точки самостоятельно, без индикатора UI.
AUTO_FIRE_REQUIRE_PENETRATION = True

# Минимальный запас пробития относительно эффективной брони точки.
AUTO_FIRE_PENETRATION_MARGIN = 0.0


# ---------------------------------------------------------------------------
# Global target state
# ---------------------------------------------------------------------------

_target = None
_target_id = None
_target_point = None
_armor_point_update_time = -999.0

# Moving-target armor lock. The selected point is stored in target-local
# coordinates so lateral movement/rotation does not make the aim jump between
# the 3x3x3 sampled armor candidates on every update.
_armor_point_local = None
_armor_point_part = None
_armor_point_target_id = None
_armor_point_last_visibility_check = -999.0
_armor_point_armor = None
_armor_point_hit_cos = None
_armor_point_part_id = None

# Last adaptive armor re-evaluation and speed-state signature.
_armor_point_last_reselect = -999.0
_armor_point_own_stationary = None
_armor_point_target_stationary = None
_armor_point_last_aspect_angle = None

_rotator = None
_original_shot_point_source = None


# ---------------------------------------------------------------------------
# Global input hook state
# ---------------------------------------------------------------------------

_input_class_hooked = False
_original_input_handle_key_event = None
_rmb_was_down = False
_rmb_event_seen = False


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

def _get_camera_center_ray():
    """Return the world ray through the exact center of the camera."""
    if cameras is not None:
        try:
            return cameras.getWorldRayAndPosition()
        except Exception:
            pass

    projection = BigWorld.projection()
    near_plane = projection.nearPlane
    inv_view = Math.Matrix(BigWorld.camera().invViewMatrix)

    point = Math.Vector3(0.0, 0.0, near_plane)
    ray = inv_view.applyVector(point)
    origin = inv_view.applyPoint(Math.Vector3(0.0, 0.0, 0.0))

    return ray, origin


# ---------------------------------------------------------------------------
# Vehicle helpers
# ---------------------------------------------------------------------------

def _get_player():
    """Return the current PlayerAvatar."""
    try:
        return BigWorld.player()
    except Exception:
        return None


def _is_on_arena(player):
    """Return True only while the player is inside an active arena."""
    try:
        return bool(player.isOnArena)
    except Exception:
        return False


def _get_vehicle_position(vehicle):
    """Return the vehicle world position."""
    try:
        return Math.Vector3(vehicle.position)
    except Exception:
        try:
            return Math.Vector3(vehicle.matrix.translation)
        except Exception:
            return None


def _get_hull_target_point(vehicle):
    """Return the world-space center of the hull bounding box."""
    try:
        descriptor = vehicle.typeDescriptor
        bbox_min, bbox_max, _ = descriptor.hull.hitTester.bbox

        local_center = (
            Math.Vector3(bbox_min) +
            Math.Vector3(bbox_max)
        ) * 0.5

        local_center += Math.Vector3(
            descriptor.chassis.hullPosition
        )

        return Math.Matrix(vehicle.matrix).applyPoint(local_center)
    except Exception:
        return _get_vehicle_position(vehicle)


def _get_turret_target_point(vehicle):
    """Return an approximate world-space center of the turret."""
    try:
        descriptor = vehicle.typeDescriptor
        bbox_min, bbox_max, _ = descriptor.turret.hitTester.bbox

        local_center = (
            Math.Vector3(bbox_min) +
            Math.Vector3(bbox_max)
        ) * 0.5

        local_center += Math.Vector3(
            descriptor.hull.turretPositions[0]
        )
        local_center += Math.Vector3(
            descriptor.chassis.hullPosition
        )

        return Math.Matrix(vehicle.matrix).applyPoint(local_center)
    except Exception:
        return _get_hull_target_point(vehicle)


def _get_center_target_point(vehicle, part):
    """Return the geometric center of the requested vehicle part."""
    try:
        descriptor = vehicle.typeDescriptor

        if part == 'HULL':
            bbox_min, bbox_max, _ = descriptor.hull.hitTester.bbox
            local_center = (
                Math.Vector3(bbox_min) +
                Math.Vector3(bbox_max)
            ) * 0.5
            local_center += Math.Vector3(descriptor.chassis.hullPosition)
            return Math.Matrix(vehicle.matrix).applyPoint(local_center)

        turret_positions = descriptor.hull.turretPositions
        if not turret_positions:
            return None

        bbox_min, bbox_max, _ = descriptor.turret.hitTester.bbox
        local_center = (
            Math.Vector3(bbox_min) +
            Math.Vector3(bbox_max)
        ) * 0.5
        local_center += Math.Vector3(turret_positions[0])
        local_center += Math.Vector3(descriptor.chassis.hullPosition)

        return Math.Matrix(vehicle.matrix).applyPoint(local_center)
    except Exception:
        return _get_vehicle_position(vehicle)


def _get_hull_target_point(vehicle):
    return _get_center_target_point(vehicle, 'HULL')


def _get_turret_target_point(vehicle):
    point = _get_center_target_point(vehicle, 'TURRET')
    if point is not None:
        return point
    return _get_hull_target_point(vehicle)


def _get_part_bbox(vehicle, part):
    try:
        descriptor = vehicle.typeDescriptor
        if part == 'HULL':
            return descriptor.hull.hitTester.bbox
        return descriptor.turret.hitTester.bbox
    except Exception:
        return None


def _get_part_local_offset(vehicle, part):
    try:
        descriptor = vehicle.typeDescriptor
        if part == 'HULL':
            return Math.Vector3(descriptor.chassis.hullPosition)

        turret_positions = descriptor.hull.turretPositions
        if not turret_positions:
            return None

        return (
            Math.Vector3(descriptor.chassis.hullPosition) +
            Math.Vector3(turret_positions[0])
        )
    except Exception:
        return None


def _get_parent_part_index(vehicle, part_id):
    """Normalize dynamic collision parts to hull/turret/chassis parents."""
    try:
        if TankPartIndexes is None:
            return part_id

        if part_id == TankPartIndexes.HULL:
            return TankPartIndexes.HULL
        if part_id == TankPartIndexes.TURRET:
            return TankPartIndexes.TURRET

        collisions = vehicle.appearance.collisions
        if collisions is not None:
            parent = collisions.getParentPartIndex(part_id)
            if parent is not None:
                return parent
    except Exception:
        pass

    return part_id


def _collision_is_part(vehicle, part_id, part):
    if TankPartIndexes is None:
        return True

    normalized = _get_parent_part_index(vehicle, part_id)
    if part == 'HULL':
        return normalized == TankPartIndexes.HULL
    return normalized == TankPartIndexes.TURRET


def _world_point_for_local(vehicle, local_point):
    try:
        return Math.Matrix(vehicle.matrix).applyPoint(local_point)
    except Exception:
        return None


def _static_path_blocked(space_id, start, end):
    """Check map/static geometry between muzzle and candidate point."""
    try:
        collision = BigWorld.wg_collideSegment(
            space_id, start, end, 128)
    except Exception:
        return False

    if collision is None:
        return False

    # Static collision APIs used by the client normally return a hit object.
    # If the reported hit is effectively the requested target point, do not
    # treat the target surface itself as an obstruction.
    try:
        point = collision.closestPoint
        if (point - end).length <= ARMOR_ZONE_POINT_EPSILON:
            return False
    except Exception:
        pass

    return True


def _dynamic_path_blocked(space_id, start, end, player, target):
    """Check dynamic obstacles while explicitly ignoring our own vehicle.

    wg_collideDynamic() accepts one entity id to ignore.  The muzzle starts
    inside our own vehicle geometry, so that id must always be our vehicle,
    not the target.  The target itself is allowed as the endpoint and is
    therefore filtered separately below.
    """
    own_id = -1
    try:
        own_vehicle = player.getVehicleAttached()
        own_id = int(getattr(own_vehicle, 'id', -1))
    except Exception:
        pass

    try:
        collision = BigWorld.wg_collideDynamic(
            space_id, start, end, own_id)
    except Exception:
        return False

    if collision is None:
        return False

    try:
        entity = collision.entity
        entity_id = getattr(entity, 'id', -1)

        # Own vehicle is never a blocking object for its own shot ray.
        if entity_id == own_id:
            return False

        # Reaching the selected enemy is the expected endpoint, not a blocker.
        if target is not None and entity_id == getattr(target, 'id', -2):
            return False
    except Exception:
        # Preserve the existing conservative behavior if the collision object
        # does not expose an entity.
        pass

    return True


def _candidate_path_clear(player, target, muzzle, point):
    try:
        space_id = target.spaceID
    except Exception:
        space_id = getattr(player, 'spaceID', None)

    if space_id is None:
        return True

    if _static_path_blocked(space_id, muzzle, point):
        return False

    if _dynamic_path_blocked(space_id, muzzle, point, player, target):
        return False

    return True


def _get_collision_for_candidate(vehicle, start, end, part):
    """Return the first armor collision belonging to the requested part."""
    try:
        collisions = vehicle.appearance.collisions
        if collisions is None:
            return None

        direction = end - start
        distance = direction.length
        if distance <= 0.000001:
            return None

        direction = direction / distance
        results = collisions.collideAllWorld(start, end)
        if not results:
            return None

        for dist, hit_angle_cos, mat_kind, part_id in results:
            if not _collision_is_part(vehicle, part_id, part):
                continue

            if getMatinfo is None:
                continue

            try:
                mat_info = getMatinfo(
                    vehicle,
                    part_id,
                    mat_kind,
                    vehicle.typeDescriptor.type.isWheeledVehicle)
            except Exception:
                mat_info = None

            if mat_info is None or mat_info.armor is None:
                continue

            hit_point = start + direction * float(dist)
            return hit_point, float(hit_angle_cos), mat_info, part_id

    except Exception:
        return None

    return None


def _build_armor_candidates(player, vehicle, part, muzzle):
    """Sample a part and return visible armor collision candidates."""
    bbox = _get_part_bbox(vehicle, part)
    offset = _get_part_local_offset(vehicle, part)

    if bbox is None or offset is None:
        return []

    try:
        bbox_min = Math.Vector3(bbox[0])
        bbox_max = Math.Vector3(bbox[1])
    except Exception:
        return []

    grid = max(int(ARMOR_ZONE_GRID_SIZE), 2)
    candidates = []

    # Include the exact geometric center first: hull center remains the
    # primary reference point and gives the selector a stable fallback.
    fractions = []
    if grid == 2:
        fractions = (0.25, 0.75)
    else:
        for i in xrange(grid):
            fractions.append(float(i) / float(grid - 1))

    for fx in fractions:
        for fy in fractions:
            for fz in fractions:
                local_point = Math.Vector3(
                    bbox_min.x + (bbox_max.x - bbox_min.x) * fx,
                    bbox_min.y + (bbox_max.y - bbox_min.y) * fy,
                    bbox_min.z + (bbox_max.z - bbox_min.z) * fz)
                local_point += offset

                world_point = _world_point_for_local(vehicle, local_point)
                if world_point is None:
                    continue

                hit = _get_collision_for_candidate(
                    vehicle, muzzle, world_point, part)
                if hit is None:
                    continue

                hit_point, hit_angle_cos, mat_info, part_id = hit

                if not _candidate_path_clear(
                        player, vehicle, muzzle, hit_point):
                    continue

                armor = float(mat_info.armor or 0.0)
                hit_angle_cos = abs(float(hit_angle_cos))
                hit_angle_cos = max(
                    ARMOR_ZONE_MIN_HIT_COS,
                    min(1.0, hit_angle_cos))

                candidates.append((
                    armor,
                    hit_angle_cos,
                    hit_point,
                    part_id))

    return candidates


def _select_best_armor_from_candidates(candidates, player=None, muzzle=None):
    global _armor_point_armor
    global _armor_point_hit_cos
    global _armor_point_part_id

    """Select the point with the lowest effective armor.

    Effective armor is approximated as nominal armor divided by the
    absolute cosine of the impact angle.  Thus a thin plate hit close to
    normal (cos ~= 1) is preferred over a thicker/angled plate.  The
    existing minimum hit cosine already filters extremely oblique hits.
    """
    if not candidates:
        return None

    prepared = []
    for item in candidates:
        armor = max(float(item[0]), 0.001)
        hit_cos = max(abs(float(item[1])), ARMOR_ZONE_MIN_HIT_COS)
        hit_cos = min(hit_cos, 1.0)
        effective_armor = armor / hit_cos

        # Внутренний hit_angle_cos в клиенте соответствует углу к нормали.
        # Для пользовательского параметра 90° переводим его в угол к
        # плоскости брони: 90° = идеальная нормаль к плоскости.
        normal_angle = math.degrees(math.acos(hit_cos))
        impact_angle = 90.0 - normal_angle
        angle_error = abs(impact_angle - ARMOR_ZONE_TARGET_IMPACT_ANGLE_DEGREES)
        penetration = None
        if ARMOR_ZONE_PENETRATION_AWARE and player is not None and muzzle is not None:
            try:
                distance = (item[2] - muzzle).length
                penetration = _get_shell_piercing_at_distance(
                    player, distance)
            except Exception:
                penetration = None

        penetrable = (
            penetration is not None and
            penetration + ARMOR_ZONE_PENETRATION_MARGIN >= effective_armor)
        prepared.append((
            effective_armor, armor, angle_error, hit_cos, item[2],
            penetration, penetrable))

    if ARMOR_ZONE_PENETRATION_AWARE:
        penetrable_candidates = [value for value in prepared if value[6]]
    else:
        penetrable_candidates = []

    pool = penetrable_candidates if penetrable_candidates else prepared
    pool.sort(key=lambda value: value[0])
    best_effective = pool[0][0]
    tie_limit = best_effective * (1.0 + ARMOR_ZONE_EFFECTIVE_ARMOR_TIE_PERCENT)
    finalists = [value for value in pool if value[0] <= tie_limit]

    # При близкой эффективной броне: сначала тоньше номинальная броня,
    # затем угол как можно ближе к эталону, затем больший hit cos.
    finalists.sort(key=lambda value: (value[1], value[2], -value[3]))
    selected = finalists[0]

    _armor_point_armor = selected[1]
    _armor_point_hit_cos = selected[3]

    # Recover the collision part id from the original candidate tuple.
    selected_point = selected[4]
    _armor_point_part_id = None
    for item in candidates:
        if item[2] is selected_point or item[2] == selected_point:
            _armor_point_part_id = item[3]
            break

    return selected_point


def _get_target_speed(vehicle):
    """Return the target's current speed in world units per second."""
    velocity = _get_target_velocity(vehicle)
    if velocity is None:
        return 0.0

    try:
        return velocity.length
    except Exception:
        try:
            return math.sqrt(
                velocity.x * velocity.x +
                velocity.y * velocity.y +
                velocity.z * velocity.z)
        except Exception:
            return 0.0


def _is_target_stationary(vehicle):
    """Return True when current target speed is at or below 10 km/h."""
    return _get_target_speed(vehicle) <= STATIONARY_SPEED_MPS


def _find_best_armor_point(player, vehicle):
    """Find the best penetration point: hull first, turret second."""
    global _armor_point_part

    if not ARMOR_ZONE_ENABLED:
        return None

    muzzle = _get_muzzle_world_position(
        player, _get_gun_rotator(player))
    if muzzle is None:
        return None

    # Stationary target: evaluate every accessible candidate and choose
    # the one with the lowest effective armor. Hull always has priority;
    # turret is considered only when no hull candidate is usable.
    hull_candidates = _build_armor_candidates(
        player, vehicle, 'HULL', muzzle)

    if hull_candidates:
        _armor_point_part = 'HULL'
        return _select_best_armor_from_candidates(hull_candidates, player, muzzle)

    turret_candidates = _build_armor_candidates(
        player, vehicle, 'TURRET', muzzle)

    if turret_candidates:
        _armor_point_part = 'TURRET'
        return _select_best_armor_from_candidates(turret_candidates, player, muzzle)

    return None


def _find_available_armor_point(player, vehicle):
    """Find any currently usable armor point: hull first, turret second."""
    global _armor_point_part
    global _armor_point_armor
    global _armor_point_hit_cos
    global _armor_point_part_id

    if not ARMOR_ZONE_ENABLED:
        return None

    muzzle = _get_muzzle_world_position(
        player, _get_gun_rotator(player))
    if muzzle is None:
        return None

    # While tracking a moving target we only need one usable point.
    # Hull has absolute priority; turret is considered only when the hull
    # has no currently accessible candidate.
    hull_candidates = _build_armor_candidates(
        player, vehicle, 'HULL', muzzle)

    if hull_candidates:
        _armor_point_part = 'HULL'
        # Do not search for the global optimum here. Any accessible hull
        # point is preferable to a turret point and keeps tracking stable.
        selected = hull_candidates[0]
        _armor_point_armor = float(selected[0])
        _armor_point_hit_cos = max(
            ARMOR_ZONE_MIN_HIT_COS, min(1.0, abs(float(selected[1]))))
        _armor_point_part_id = selected[3]
        return selected[2]

    turret_candidates = _build_armor_candidates(
        player, vehicle, 'TURRET', muzzle)

    if turret_candidates:
        _armor_point_part = 'TURRET'
        selected = turret_candidates[0]
        _armor_point_armor = float(selected[0])
        _armor_point_hit_cos = max(
            ARMOR_ZONE_MIN_HIT_COS, min(1.0, abs(float(selected[1]))))
        _armor_point_part_id = selected[3]
        return selected[2]

    return None


def _get_best_armor_point(player, vehicle):
    """Choose stationary optimum or moving stable point, then hull center."""
    if _is_target_stationary(vehicle):
        point = _find_best_armor_point(player, vehicle)
    else:
        point = _find_available_armor_point(player, vehicle)

    if point is not None:
        return point

    return _get_hull_target_point(vehicle)


def _get_target_point(vehicle):
    """Return the selected armor point with hull-first priority."""
    player = _get_player()

    if player is None:
        return _get_hull_target_point(vehicle)

    try:
        return _get_best_armor_point(player, vehicle)
    except Exception:
        return _get_hull_target_point(vehicle)


# ---------------------------------------------------------------------------
# Search geometry
# ---------------------------------------------------------------------------

def _normalise_vector(vector):
    """Return a normalized vector or None for a zero vector."""
    length = vector.length

    if length <= 0.000001:
        return None

    return vector / length


def _inside_search_cone(origin, ray, vehicle_position):
    """Return True when the vehicle is inside the camera search cone."""
    direction = vehicle_position - origin
    distance = direction.length

    if distance <= 0.000001:
        return False

    direction = direction / distance

    cosine = ray.dot(direction)
    cosine = max(-1.0, min(1.0, cosine))

    return math.degrees(math.acos(cosine)) <= SEARCH_ANGLE_DEGREES


# ---------------------------------------------------------------------------
# Target acquisition
# ---------------------------------------------------------------------------

def _find_target():
    """Find the nearest live enemy inside the camera search cone."""
    player = _get_player()

    if player is None or not _is_on_arena(player):
        return None, None, None

    try:
        own_vehicle = player.getVehicleAttached()
    except Exception:
        own_vehicle = None

    if own_vehicle is None:
        return None, None, None

    try:
        if not own_vehicle.isAlive():
            return None, None, None
    except Exception:
        return None, None, None

    own_position = _get_vehicle_position(own_vehicle)

    if own_position is None:
        return None, None, None

    ray, ray_origin = _get_camera_center_ray()
    ray = _normalise_vector(ray)

    if ray is None:
        return None, None, None

    try:
        arena_vehicles = player.arena.vehicles
        vehicle_ids = arena_vehicles.keys()
    except Exception:
        return None, None, None

    player_id = getattr(player, 'id', None)
    player_team = getattr(player, 'team', None)

    best_vehicle = None
    best_vehicle_id = None
    best_distance = TARGET_MAX_DISTANCE

    for vehicle_id in vehicle_ids:
        if vehicle_id == player_id:
            continue

        try:
            arena_info = arena_vehicles[vehicle_id]

            if not arena_info.get('isAlive', False):
                continue

            if arena_info.get('team') == player_team:
                continue

            vehicle = BigWorld.entity(vehicle_id)

            if vehicle is None:
                continue

            vehicle_position = _get_vehicle_position(vehicle)

            if vehicle_position is None:
                continue

            distance = (
                vehicle_position - own_position
            ).length

            if distance > best_distance:
                continue

            if not _inside_search_cone(
                    ray_origin,
                    ray,
                    vehicle_position):
                continue

            best_distance = distance
            best_vehicle = vehicle
            best_vehicle_id = vehicle_id

        except Exception:
            continue

    if best_vehicle is None:
        return None, None, None

    return (
        best_vehicle,
        best_vehicle_id,
        _get_target_point(best_vehicle)
    )


# ---------------------------------------------------------------------------
# Gun tracking
# ---------------------------------------------------------------------------

def _get_gun_rotator(player):
    """Return the active VehicleGunRotator instance."""
    try:
        rotator = player.gunRotator

        if rotator is not None:
            return rotator
    except Exception:
        pass

    try:
        rotator = player.inputHandler.gunRotator

        if rotator is not None:
            return rotator
    except Exception:
        pass

    try:
        rotator = player.inputHandler.ctrl.gunRotator

        if rotator is not None:
            return rotator
    except Exception:
        pass

    return None


def _get_vehicle_descriptor(player):
    """Return the current vehicle descriptor."""
    try:
        descriptor = player.getVehicleDescriptor()
    except Exception:
        descriptor = getattr(player, 'vehicleTypeDescriptor', None)
    return descriptor


def _get_shot_value(shot, name, default=None):
    """Read a shot descriptor field from attribute- or mapping-style APIs."""
    try:
        return getattr(shot, name)
    except Exception:
        pass

    try:
        return shot[name]
    except Exception:
        return default


def _get_current_shot(descriptor):
    """Return the shell descriptor selected by the current vehicle."""
    if descriptor is None:
        return None

    try:
        shot = descriptor.shot
        if shot is not None:
            return shot
    except Exception:
        pass

    return None


def _get_muzzle_world_position(player, rotator=None):
    """Rebuild the same muzzle position used by VehicleGunRotator."""
    descriptor = _get_vehicle_descriptor(player)
    if descriptor is None:
        return None

    try:
        turret_positions = descriptor.hull.turretPositions
        if not turret_positions:
            return None
        turret_position = turret_positions[0]
        hull_position = descriptor.chassis.hullPosition
        gun_offset = descriptor.activeGunShotPosition
    except Exception:
        return None

    try:
        vehicle_matrix = Math.Matrix(
            player.getOwnVehicleStabilisedMatrix())
    except Exception:
        return None

    turret_yaw = 0.0
    if rotator is None:
        rotator = _get_gun_rotator(player)
    if rotator is not None:
        try:
            turret_yaw = float(rotator.turretYaw)
        except Exception:
            turret_yaw = 0.0

    turret_world_matrix = Math.Matrix()
    turret_world_matrix.setRotateY(turret_yaw)
    turret_world_matrix.translation = (
        turret_position + hull_position)
    turret_world_matrix.postMultiply(vehicle_matrix)

    return turret_world_matrix.applyPoint(gun_offset)


def _get_shell_parameters(player, rotator=None):
    """Return (speed, gravity) for the currently selected shell."""
    descriptor = _get_vehicle_descriptor(player)
    shot = _get_current_shot(descriptor)
    if shot is None:
        return None, None

    speed = _get_shot_value(shot, 'speed')
    gravity = _get_shot_value(shot, 'gravity')

    try:
        speed = float(speed)
        gravity = abs(float(gravity))
    except (TypeError, ValueError):
        return None, None

    if BALLISTIC_USE_SPEED_FACTOR:
        try:
            from items import vehicles
            factor = float(vehicles.g_cache.commonConfig['miscParams']['projectileSpeedFactor'])
        except Exception:
            factor = 1.0

        if factor > 0.0:
            speed /= factor
            gravity /= factor * factor

    if speed < BALLISTIC_MIN_SPEED or gravity < BALLISTIC_MIN_GRAVITY:
        return None, None

    return speed, gravity


def _solve_low_ballistic_angle(speed, gravity, horizontal_range, height_delta):
    """Solve the low ballistic trajectory angle without using game aiming APIs."""
    if horizontal_range < BALLISTIC_MIN_RANGE:
        return None

    v2 = speed * speed
    g = gravity
    r = horizontal_range
    h = height_delta

    discriminant = (
        v2 * v2 -
        g * (g * r * r + 2.0 * h * v2)
    )

    if discriminant < 0.0:
        return None

    root = math.sqrt(discriminant)
    denominator = g * r
    if abs(denominator) < 0.000001:
        return None

    tan_theta = (v2 - root) / denominator
    return math.atan(tan_theta)


def _get_ballistic_aim_point(player, target_point, rotator=None):
    """Return a point above/below target so direct gun geometry has ballistic pitch."""
    if not BALLISTIC_ENABLED or target_point is None:
        return target_point

    speed, gravity = _get_shell_parameters(player, rotator)
    if speed is None or gravity is None:
        return target_point

    muzzle = _get_muzzle_world_position(player, rotator)
    if muzzle is None:
        return target_point

    dx = target_point.x - muzzle.x
    dz = target_point.z - muzzle.z
    horizontal_range = math.sqrt(dx * dx + dz * dz)
    height_delta = target_point.y - muzzle.y

    if horizontal_range < BALLISTIC_MIN_RANGE:
        return target_point

    angle = _solve_low_ballistic_angle(
        speed, gravity, horizontal_range, height_delta)
    if angle is None:
        return target_point

    # Reverse-check the solved trajectory at the target range.
    # This verifies that the selected speed/gravity produce the requested
    # target height with the same low-angle solution.
    cos_theta = math.cos(angle)
    cos_sq = cos_theta * cos_theta
    if cos_sq <= 0.000001:
        return target_point

    tan_theta = math.tan(angle)
    trajectory_y = (
        muzzle.y +
        horizontal_range * tan_theta -
        (gravity * horizontal_range * horizontal_range) /
        (2.0 * speed * speed * cos_sq)
    )

    trajectory_error = trajectory_y - target_point.y

    if abs(trajectory_error) > BALLISTIC_VALIDATION_TOLERANCE:
        return target_point

    aim_y = muzzle.y + horizontal_range * tan_theta
    correction = aim_y - target_point.y

    if correction > BALLISTIC_MAX_CORRECTION:
        correction = BALLISTIC_MAX_CORRECTION
    elif correction < -BALLISTIC_MAX_CORRECTION:
        correction = -BALLISTIC_MAX_CORRECTION

    return Math.Vector3(
        target_point.x,
        target_point.y + correction,
        target_point.z)


def _get_target_velocity(vehicle):
    """Return the target's current world-space velocity."""
    if vehicle is None:
        return None

    try:
        return Math.Vector3(vehicle.velocity)
    except Exception:
        return None


def _clamp_lead_time(value):
    if value < BALLISTIC_LEAD_MIN_TIME:
        return BALLISTIC_LEAD_MIN_TIME
    if value > BALLISTIC_LEAD_MAX_TIME:
        return BALLISTIC_LEAD_MAX_TIME
    return value


def _get_leaded_target_point(player, target_point, rotator=None):
    """Predict target position from radial/lateral velocity and flight time."""
    if not BALLISTIC_LEAD_ENABLED or target_point is None:
        return target_point

    if _target is None:
        return target_point

    velocity = _get_target_velocity(_target)
    if velocity is None:
        return target_point

    # Lead is based on relative horizontal motion, not target motion alone.
    # This is important when our own vehicle is moving across the target:
    # the projectile and the target are both moving in world space, so the
    # required lateral correction depends on target velocity minus our
    # vehicle velocity.
    try:
        own_vehicle = player.getVehicleAttached()
        own_velocity = getattr(own_vehicle, 'velocity', None)
        if own_velocity is not None:
            velocity = Math.Vector3(
                velocity.x - own_velocity.x,
                velocity.y - own_velocity.y,
                velocity.z - own_velocity.z)
    except Exception:
        pass

    try:
        if velocity.lengthSquared <= 0.000001:
            return target_point
    except Exception:
        return target_point

    muzzle = _get_muzzle_world_position(player, rotator)
    if muzzle is None:
        return target_point

    speed, gravity = _get_shell_parameters(player, rotator)
    if speed is None or gravity is None:
        return target_point

    # Work in the horizontal XZ plane for the time-of-flight calculation.
    # This separates motion toward/away from the gun from motion across the
    # firing line. Lateral motion is the component that most strongly causes
    # the familiar "behind a fast crossing target" error.
    dx = target_point.x - muzzle.x
    dz = target_point.z - muzzle.z
    horizontal_range = math.sqrt(dx * dx + dz * dz)
    if horizontal_range < BALLISTIC_MIN_RANGE:
        return target_point

    line_x = dx / horizontal_range
    line_z = dz / horizontal_range

    radial_speed = (
        velocity.x * line_x +
        velocity.z * line_z
    )

    radial_x = radial_speed * line_x
    radial_z = radial_speed * line_z

    lateral_x = velocity.x - radial_x
    lateral_z = velocity.z - radial_z

    flight_time = _clamp_lead_time(horizontal_range / speed)
    predicted = target_point

    for _ in xrange(BALLISTIC_LEAD_ITERATIONS):
        # Predict only from projectile flight time. Radial and lateral
        # components have independent configurable lead factors.
        radial_time = flight_time * BALLISTIC_LEAD_RADIAL_FACTOR
        lateral_time = flight_time * BALLISTIC_LEAD_LATERAL_FACTOR

        predicted = Math.Vector3(
            target_point.x + radial_x * radial_time +
            lateral_x * lateral_time,
            target_point.y + velocity.y * flight_time,
            target_point.z + radial_z * radial_time +
            lateral_z * lateral_time)

        pdx = predicted.x - muzzle.x
        pdz = predicted.z - muzzle.z
        predicted_range = math.sqrt(pdx * pdx + pdz * pdz)
        if predicted_range < BALLISTIC_MIN_RANGE:
            break

        height_delta = predicted.y - muzzle.y
        angle = _solve_low_ballistic_angle(
            speed, gravity, predicted_range, height_delta)
        if angle is None:
            return target_point

        cos_theta = math.cos(angle)
        if abs(cos_theta) < 0.000001:
            return target_point

        new_time = predicted_range / (speed * cos_theta)
        new_time = _clamp_lead_time(new_time)

        if abs(new_time - flight_time) < BALLISTIC_LEAD_EPSILON:
            flight_time = new_time
            break

        flight_time = new_time

    # Rebuild once from the converged time using the same independent
    # radial/lateral factors. Vertical motion remains tied to actual flight
    # time and is not artificially amplified.
    radial_time = flight_time * BALLISTIC_LEAD_RADIAL_FACTOR
    lateral_time = flight_time * BALLISTIC_LEAD_LATERAL_FACTOR
    predicted = Math.Vector3(
        target_point.x + radial_x * radial_time +
        lateral_x * lateral_time,
        target_point.y + velocity.y * flight_time,
        target_point.z + radial_z * radial_time +
        lateral_z * lateral_time)

    return predicted

def _get_tracking_point(player, target_point, rotator=None):
    """Build the final point consumed by VehicleGunRotator."""
    try:
        lead_point = _get_leaded_target_point(
            player, target_point, rotator)
        return _get_ballistic_aim_point(
            player, lead_point, rotator)
    except Exception:
        return target_point

def _install_shot_point_source():
    """Install the selected target as the common gun shot-point source."""
    global _rotator
    global _original_shot_point_source

    player = _get_player()

    if player is None:
        return False

    rotator = _get_gun_rotator(player)

    if rotator is None:
        return False

    if _rotator is rotator:
        return True

    _rotator = rotator

    try:
        _original_shot_point_source = (
            rotator.shotPointSourceFunctor
        )
    except Exception:
        _original_shot_point_source = None

    def custom_shot_point_source():
        if _target is not None and _target_point is not None:
            p = _get_player()
            if p is not None:
                return _get_tracking_point(
                    p, _target_point, rotator)

        if _original_shot_point_source is not None:
            try:
                return _original_shot_point_source()
            except Exception:
                pass

        return None

    try:
        rotator.shotPointSourceFunctor = custom_shot_point_source
        return True
    except Exception:
        _rotator = None
        _original_shot_point_source = None
        return False


def _restore_shot_point_source():
    """Restore the original VehicleGunRotator shot-point source."""
    global _rotator
    global _original_shot_point_source

    if _rotator is None:
        return

    if _original_shot_point_source is not None:
        try:
            _rotator.shotPointSourceFunctor = (
                _original_shot_point_source
            )
        except Exception:
            pass

    _rotator = None
    _original_shot_point_source = None


# ---------------------------------------------------------------------------
# Stable armor-point lock for moving targets
# ---------------------------------------------------------------------------

def _world_to_vehicle_local(vehicle, world_point):
    """Convert a world point into the target vehicle's local coordinates."""
    try:
        matrix = Math.Matrix(vehicle.matrix)
        matrix.invert()
        return matrix.applyPoint(world_point)
    except Exception:
        return None


def _vehicle_local_to_world(vehicle, local_point):
    """Convert a target-local point back into world coordinates."""
    try:
        return Math.Matrix(vehicle.matrix).applyPoint(local_point)
    except Exception:
        return None


def _lock_armor_point(vehicle, point, part):
    """Remember one armor hit point in target-local space."""
    global _armor_point_local
    global _armor_point_part
    global _armor_point_target_id

    if vehicle is None or point is None:
        return False

    local_point = _world_to_vehicle_local(vehicle, point)
    if local_point is None:
        return False

    _armor_point_local = local_point
    _armor_point_part = part
    _armor_point_target_id = getattr(vehicle, 'id', None)
    return True


def _clear_armor_point_lock():
    global _armor_point_local
    global _armor_point_part
    global _armor_point_target_id
    global _armor_point_last_visibility_check
    global _armor_point_armor
    global _armor_point_hit_cos
    global _armor_point_part_id
    global _armor_point_last_reselect
    global _armor_point_own_stationary
    global _armor_point_target_stationary
    global _armor_point_last_aspect_angle

    _armor_point_local = None
    _armor_point_part = None
    _armor_point_target_id = None
    _armor_point_last_visibility_check = -999.0
    _armor_point_armor = None
    _armor_point_hit_cos = None
    _armor_point_part_id = None
    _armor_point_last_reselect = -999.0
    _armor_point_own_stationary = None
    _armor_point_target_stationary = None
    _armor_point_last_aspect_angle = None


def _get_locked_armor_point(player, vehicle):
    """Return the locked point if it is still a valid accessible armor hit."""
    if vehicle is None or _armor_point_local is None:
        return None

    if (_armor_point_target_id is not None and
            _armor_point_target_id != getattr(vehicle, 'id', None)):
        return None

    point = _vehicle_local_to_world(vehicle, _armor_point_local)
    if point is None:
        return None

    muzzle = _get_muzzle_world_position(
        player, _get_gun_rotator(player))
    if muzzle is None:
        return None

    # The point is already on the target's rigid local surface. We only need
    # to verify that the route is not blocked by map geometry/another entity.
    if not _candidate_path_clear(player, vehicle, muzzle, point):
        return None

    return point


def _get_locked_effective_armor():
    """Return the effective armor of the currently locked sampled point."""
    if _armor_point_armor is None or _armor_point_hit_cos is None:
        return None
    try:
        return float(_armor_point_armor) / max(
            float(_armor_point_hit_cos), ARMOR_ZONE_MIN_HIT_COS)
    except Exception:
        return None


def _candidate_effective_armor_at_point(player, vehicle, point, part):
    """Re-evaluate armor metadata for one already locked world point."""
    muzzle = _get_muzzle_world_position(
        player, _get_gun_rotator(player))
    if muzzle is None:
        return None

    hit = _get_collision_for_candidate(vehicle, muzzle, point, part)
    if hit is None:
        return None

    hit_point, hit_cos, mat_info, part_id = hit
    if not _candidate_path_clear(player, vehicle, muzzle, hit_point):
        return None

    try:
        armor = max(float(mat_info.armor or 0.0), 0.001)
        hit_cos = max(ARMOR_ZONE_MIN_HIT_COS,
                       min(1.0, abs(float(hit_cos))))
        return armor / hit_cos
    except Exception:
        return None


def _get_target_aspect_angle(player, vehicle):
    """Return the target aspect angle relative to our horizontal firing line."""
    if player is None or vehicle is None:
        return None

    try:
        muzzle = _get_muzzle_world_position(
            player, _get_gun_rotator(player))
        if muzzle is None:
            return None

        target_pos = _get_vehicle_position(vehicle)
        if target_pos is None:
            return None

        to_shooter = Math.Vector3(
            muzzle.x - target_pos.x, 0.0, muzzle.z - target_pos.z)
        length = math.sqrt(
            to_shooter.x * to_shooter.x +
            to_shooter.z * to_shooter.z)
        if length <= 0.001:
            return None
        to_shooter.x /= length
        to_shooter.z /= length

        try:
            forward = Math.Matrix(vehicle.matrix).applyVector(
                Math.Vector3(0.0, 0.0, 1.0))
        except Exception:
            return None

        forward.y = 0.0
        forward_length = math.sqrt(
            forward.x * forward.x + forward.z * forward.z)
        if forward_length <= 0.001:
            return None
        forward.x /= forward_length
        forward.z /= forward_length

        dot = max(-1.0, min(1.0,
            forward.x * to_shooter.x + forward.z * to_shooter.z))
        return math.degrees(math.acos(dot))
    except Exception:
        return None


def _should_reselect_armor_point(player, vehicle, now):
    """Decide whether the current lock should be reconsidered.

    A full armor scan is triggered by meaningful state changes: moving/stopped
    transitions, a significant target-aspect change, or a slow fallback timer.
    This avoids doing the expensive 3x3x3 collision scan just because time passed.
    """
    global _armor_point_last_reselect
    global _armor_point_own_stationary
    global _armor_point_target_stationary
    global _armor_point_last_aspect_angle

    own_stationary = _is_own_vehicle_stationary(player)
    target_stationary = _is_target_stationary(vehicle)
    aspect_angle = _get_target_aspect_angle(player, vehicle)

    state_changed = (
        _armor_point_own_stationary is not None and
        _armor_point_own_stationary != own_stationary
    ) or (
        _armor_point_target_stationary is not None and
        _armor_point_target_stationary != target_stationary
    )

    aspect_changed = False
    if aspect_angle is not None and _armor_point_last_aspect_angle is not None:
        try:
            aspect_changed = abs(
                aspect_angle - _armor_point_last_aspect_angle
            ) >= float(ARMOR_ZONE_ASPECT_RESELECT_DEGREES)
        except Exception:
            aspect_changed = False

    _armor_point_own_stationary = own_stationary
    _armor_point_target_stationary = target_stationary
    if aspect_angle is not None:
        _armor_point_last_aspect_angle = aspect_angle

    if state_changed or aspect_changed:
        _armor_point_last_reselect = now
        return True

    if now - _armor_point_last_reselect >= ARMOR_ZONE_RESELECT_INTERVAL:
        _armor_point_last_reselect = now
        return True

    return False

def _try_adaptive_armor_reselection(player, vehicle, now):
    """Search for a materially better exposed armor point without jitter."""
    global _armor_point_part
    global _armor_point_armor
    global _armor_point_hit_cos
    global _armor_point_part_id
    global _target_point

    if not ARMOR_ZONE_ENABLED or _armor_point_local is None:
        return False

    if not _should_reselect_armor_point(player, vehicle, now):
        return False

    current_point = _vehicle_local_to_world(vehicle, _armor_point_local)
    if current_point is None:
        return False

    old_part = _armor_point_part
    old_armor = _armor_point_armor
    old_hit_cos = _armor_point_hit_cos
    old_part_id = _armor_point_part_id
    # Re-evaluate the currently locked surface from the current muzzle
    # position. Target rotation can change its impact angle without changing
    # the local point itself.
    current_effective = _candidate_effective_armor_at_point(
        player, vehicle, current_point, _armor_point_part)
    if current_effective is None:
        return False

    # Adaptive reselection deliberately uses the full candidate ranking even
    # for a moving target. This is not done every frame; the throttle below
    # makes it a controlled re-evaluation rather than the old jitter loop.
    candidate = _find_best_armor_point(player, vehicle)

    if candidate is None:
        _armor_point_part = old_part
        _armor_point_armor = old_armor
        _armor_point_hit_cos = old_hit_cos
        _armor_point_part_id = old_part_id
        return False

    candidate_part = _armor_point_part
    candidate_armor = _armor_point_armor
    candidate_hit_cos = _armor_point_hit_cos
    candidate_part_id = _armor_point_part_id
    new_effective = _get_locked_effective_armor()

    try:
        improvement_percent = max(0.0, float(
            ARMOR_ZONE_RESELECT_IMPROVEMENT_PERCENT))
    except Exception:
        improvement_percent = 0.0

    required = current_effective * (1.0 - improvement_percent / 100.0)

    # If the new candidate is not materially better, restore the old point's
    # metadata and keep the existing local lock exactly where it is.
    if new_effective is None or new_effective >= required:
        _armor_point_part = old_part
        _armor_point_armor = old_armor
        _armor_point_hit_cos = old_hit_cos
        _armor_point_part_id = old_part_id
        return False

    # Accept the new candidate and convert it to a stable local-space lock.
    if not _lock_armor_point(vehicle, candidate, candidate_part):
        _armor_point_part = old_part
        _armor_point_armor = old_armor
        _armor_point_hit_cos = old_hit_cos
        _armor_point_part_id = old_part_id
        return False

    _armor_point_armor = candidate_armor
    _armor_point_hit_cos = candidate_hit_cos
    _armor_point_part_id = candidate_part_id
    _target_point = candidate
    return True


def _refresh_armor_point_for_state(player, vehicle):
    """Select/reselect one armor point; do not repeatedly rescan while locked."""
    global _target_point
    global _armor_point_update_time
    global _armor_point_last_reselect
    global _armor_point_own_stationary
    global _armor_point_target_stationary

    if not ARMOR_ZONE_ENABLED:
        _clear_armor_point_lock()
        _target_point = _get_hull_target_point(vehicle)
        _armor_point_update_time = BigWorld.time()
        return

    locked_point = _vehicle_local_to_world(
        vehicle, _armor_point_local) if _armor_point_local is not None else None
    if locked_point is not None:
        _target_point = locked_point
        _armor_point_update_time = BigWorld.time()
        return

    if _is_target_stationary(vehicle):
        point = _find_best_armor_point(player, vehicle)
    else:
        point = _find_available_armor_point(player, vehicle)

    if point is not None:
        part = _armor_point_part
        if part is None:
            part = 'HULL'
        if _lock_armor_point(vehicle, point, part):
            _target_point = point
        else:
            _clear_armor_point_lock()
            _target_point = _get_hull_target_point(vehicle)
    else:
        _clear_armor_point_lock()
        _target_point = _get_hull_target_point(vehicle)

    _armor_point_update_time = BigWorld.time()
    _armor_point_own_stationary = _is_own_vehicle_stationary(player)
    _armor_point_target_stationary = _is_target_stationary(vehicle)
    _armor_point_last_reselect = _armor_point_update_time


# ---------------------------------------------------------------------------
# Target lifecycle
# ---------------------------------------------------------------------------

def _clear_target():
    """Clear the selected target and release gun tracking."""
    global _target
    global _target_id
    global _target_point

    global _auto_fire_last_attempt

    _target = None
    _target_id = None
    _target_point = None
    _clear_armor_point_lock()
    _auto_fire_last_attempt = -999.0

    _restore_shot_point_source()


def _set_target(vehicle, vehicle_id, target_point):
    """Set the selected target and install gun tracking."""
    global _target
    global _target_id
    global _target_point
    global _armor_point_update_time
    global _auto_fire_last_attempt

    _target = vehicle
    _target_id = vehicle_id
    _target_point = target_point
    _armor_point_update_time = -999.0
    _auto_fire_last_attempt = -999.0
    _clear_armor_point_lock()

    if not _install_shot_point_source():
        _clear_target()
        return False

    return True


def _validate_target():
    """Validate target and keep one continuous armor point while it moves."""
    global _target_point
    global _armor_point_last_visibility_check

    if _target is None:
        return False

    player = _get_player()

    if player is None or not _is_on_arena(player):
        _clear_target()
        return False

    try:
        arena_info = player.arena.vehicles[_target_id]

        if not arena_info.get('isAlive', False):
            _clear_target()
            return False

        if arena_info.get('team') == getattr(player, 'team', None):
            _clear_target()
            return False
    except Exception:
        _clear_target()
        return False

    try:
        target_position = _get_vehicle_position(_target)
        player_position = _get_vehicle_position(player)

        if target_position is None or player_position is None:
            _clear_target()
            return False

        if REQUIRE_DISTANCE_WHILE_TRACKING:
            distance = (target_position - player_position).length
            if distance > TARGET_MAX_DISTANCE:
                _clear_target()
                return False
    except Exception:
        _clear_target()
        return False

    # The target-local point is reprojected every tick, but collision checks
    # and expensive 3x3x3 reselection are throttled. This keeps lateral
    # tracking continuous and prevents repeated full scans while hidden.
    now = BigWorld.time()
    locked_point = None
    if ARMOR_ZONE_ENABLED and _armor_point_local is not None:
        locked_point = _vehicle_local_to_world(_target, _armor_point_local)

    if locked_point is not None:
        # Adaptive reselection is throttled and hysteretic: a new point must
        # materially improve effective armor before replacing the current lock.
        if _try_adaptive_armor_reselection(player, _target, now):
            locked_point = _vehicle_local_to_world(_target, _armor_point_local)

        if now - _armor_point_last_visibility_check >= ARMOR_ZONE_VISIBILITY_CHECK_INTERVAL:
            muzzle = _get_muzzle_world_position(
                player, _get_gun_rotator(player))
            if muzzle is not None and _candidate_path_clear(
                    player, _target, muzzle, locked_point):
                _target_point = locked_point
                _armor_point_last_visibility_check = now
            else:
                _clear_armor_point_lock()
                _refresh_armor_point_for_state(player, _target)
        else:
            _target_point = locked_point
    else:
        if (not ARMOR_ZONE_ENABLED or
                now - _armor_point_update_time >= ARMOR_ZONE_UPDATE_INTERVAL or
                _target_point is None):
            _refresh_armor_point_for_state(player, _target)
    if _target_point is None:
        _clear_target()
        return False

    _install_shot_point_source()
    return True


def _acquire_target():
    """Acquire a target using the current free camera direction."""
    player = _get_player()

    if player is None or not _is_on_arena(player):
        return False

    try:
        own_vehicle = player.getVehicleAttached()
    except Exception:
        own_vehicle = None

    if own_vehicle is None:
        return False

    try:
        if not own_vehicle.isAlive():
            return False
    except Exception:
        return False

    vehicle, vehicle_id, target_point = _find_target()

    if vehicle is None or target_point is None:
        return False

    if not _set_target(vehicle, vehicle_id, target_point):
        return False

    return True


# ---------------------------------------------------------------------------
# Common input hook / physical RMB polling
# ---------------------------------------------------------------------------

def _process_rmb(is_down, key, is_repeat):
    """Process RMB when the engine delivers it as a key event."""
    global _rmb_event_seen

    if is_repeat or not is_down or key != MOUSE_BUTTON:
        return False

    _rmb_event_seen = True

    if _target is not None:
        _clear_target()
        return True

    _acquire_target()
    return True


def _poll_rmb():
    """Catch the physical RMB state independently of control/camera mode."""
    global _rmb_was_down
    global _rmb_event_seen

    try:
        is_down = bool(BigWorld.isKeyDown(MOUSE_BUTTON))
    except Exception:
        is_down = False

    # Rising edge only: holding RMB must not repeatedly toggle the target.
    pressed = is_down and not _rmb_was_down
    _rmb_was_down = is_down

    if not is_down:
        _rmb_event_seen = False
        return

    if _rmb_event_seen or not pressed:
        return

    if _target is not None:
        _clear_target()
        return

    _acquire_target()


def _install_class_input_hook():
    """
    Keep the event hook as a fast path for modes where RMB arrives here.
    Arcade is additionally covered by physical-key polling in _update().
    """
    global _input_class_hooked
    global _original_input_handle_key_event

    if _input_class_hooked:
        return True

    if AvatarInputHandler is None:
        return False

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
    """Restore the original AvatarInputHandler class method."""
    global _input_class_hooked
    global _original_input_handle_key_event

    if not _input_class_hooked:
        return

    if AvatarInputHandler is not None:
        try:
            input_class = AvatarInputHandler.AvatarInputHandler
            if _original_input_handle_key_event is not None:
                input_class.handleKeyEvent = _original_input_handle_key_event
        except Exception:
            pass

    _input_class_hooked = False
    _original_input_handle_key_event = None


# ---------------------------------------------------------------------------
# Automatic fire
# ---------------------------------------------------------------------------

_auto_fire_last_attempt = -999.0

def _get_vehicle_speed(vehicle):
    velocity = _get_target_velocity(vehicle)
    if velocity is None:
        return 0.0
    try:
        return velocity.length
    except Exception:
        try:
            return math.sqrt(
                velocity.x * velocity.x +
                velocity.y * velocity.y +
                velocity.z * velocity.z)
        except Exception:
            return 0.0


def _is_own_vehicle_stationary(player):
    try:
        vehicle = player.getVehicleAttached()
    except Exception:
        vehicle = None
    return vehicle is not None and _get_vehicle_speed(vehicle) <= STATIONARY_SPEED_MPS


def _is_target_stationary_for_fire():
    return _target is not None and _get_target_speed(_target) <= STATIONARY_SPEED_MPS


def _is_gun_fully_aimed(player, rotator):
    """Check current dispersion against the gun's fully aimed value.

    The fully aimed reference is the static gun descriptor value
    ``gun.shotDispersionAngle``.  The current value is the live
    ``VehicleGunRotator.dispersionAngle``.  They are considered matched
    when the current value is inside the configured +/- percentage window.
    """
    if rotator is None:
        return False

    try:
        descriptor = _get_vehicle_descriptor(player)
        gun = descriptor.gun
        full_dispersion = float(gun.shotDispersionAngle)
    except Exception:
        return False

    try:
        current_dispersion = float(rotator.dispersionAngle)
    except Exception:
        return False

    if full_dispersion <= 0.0 or current_dispersion < 0.0:
        return False

    try:
        tolerance_percent = max(0.0, float(
            AUTO_FIRE_FULL_AIM_TOLERANCE_PERCENT))
    except Exception:
        tolerance_percent = 0.0

    factor = tolerance_percent / 100.0
    lower_bound = full_dispersion * (1.0 - factor)
    upper_bound = full_dispersion * (1.0 + factor)

    return (
        current_dispersion >= lower_bound and
        current_dispersion <= upper_bound
    )


def _get_current_fire_point(player, rotator):
    """Return the exact tracked point after lead/ballistic correction."""
    if _target_point is None:
        return None

    try:
        return _get_tracking_point(player, _target_point, rotator)
    except Exception:
        return _target_point


def _is_current_fire_point_clear(player, target, point):
    """Use the already validated armor point as the visibility source.

    Do not raycast the ballistic/lead point here: that point can be vertically
    offset from the armor surface by the projectile correction.  Visibility
    of the selected armor point is already throttled in _validate_target().
    """
    if not AUTO_FIRE_REQUIRE_CLEAR_PATH:
        return True

    if target is None or _target_point is None:
        return False

    try:
        muzzle = _get_muzzle_world_position(
            player, _get_gun_rotator(player))
    except Exception:
        muzzle = None

    if muzzle is None:
        return False

    return _candidate_path_clear(
        player, target, muzzle, _target_point)


def _get_shell_piercing_at_distance(player, distance):
    """Return shell piercing power at the current range."""
    descriptor = _get_vehicle_descriptor(player)
    shot = _get_current_shot(descriptor)
    if shot is None:
        return None

    try:
        piercing = shot.piercingPower
        base = float(piercing[0])
    except Exception:
        return None

    try:
        from helpers_common import computePiercingPowerAtDist
        return float(computePiercingPowerAtDist(piercing, distance))
    except Exception:
        return base


def _is_current_point_penetrable(player, target, point):
    """Check penetration using the armor data captured for the locked point."""
    if not AUTO_FIRE_REQUIRE_PENETRATION:
        return True

    if target is None or _target_point is None:
        return False

    if _armor_point_armor is None or _armor_point_hit_cos is None:
        # Center-hull fallback has no sampled material data, so it is not
        # considered a verified armor point for automatic fire.
        return False

    effective_armor = float(_armor_point_armor) / max(
        float(_armor_point_hit_cos), ARMOR_ZONE_MIN_HIT_COS)

    try:
        muzzle = _get_muzzle_world_position(
            player, _get_gun_rotator(player))
        if muzzle is None:
            return False
        distance = (_target_point - muzzle).length
    except Exception:
        return False

    penetration = _get_shell_piercing_at_distance(player, distance)
    if penetration is None:
        return False

    return penetration + AUTO_FIRE_PENETRATION_MARGIN >= effective_armor


def _can_auto_fire(player):
    if not AUTO_FIRE_ENABLED or _target is None:
        return False

    if AUTO_FIRE_TARGET_STATIONARY_ONLY and not _is_target_stationary_for_fire():
        return False

    if AUTO_FIRE_OWN_STATIONARY_ONLY and not _is_own_vehicle_stationary(player):
        return False

    rotator = _get_gun_rotator(player)
    if rotator is None:
        return False

    if not _is_gun_fully_aimed(player, rotator):
        return False

    # Intentionally no control-mode/camera restriction and no _canShot gate.
    # VehicleGunRotator already performs lead/ballistic tracking for the actual
    # gun. Recomputing that 8-iteration calculation here would only waste CPU.
    if _target_point is None:
        return False

    # Visibility is already checked by _validate_target() every
    # ARMOR_ZONE_VISIBILITY_CHECK_INTERVAL seconds. Repeating the same
    # collision query here at every 0.05 s update only adds CPU load.
    if AUTO_FIRE_REQUIRE_CLEAR_PATH:
        if _target_point is None:
            return False
        if _armor_point_local is not None:
            if (BigWorld.time() - _armor_point_last_visibility_check >
                    ARMOR_ZONE_VISIBILITY_CHECK_INTERVAL):
                return False

    if not _is_current_point_penetrable(player, _target, _target_point):
        return False

    return True

def _process_auto_fire(player):
    """Fire only after every configured safety/aim condition is satisfied."""
    global _auto_fire_last_attempt

    if not _can_auto_fire(player):
        return

    now = BigWorld.time()
    if now - _auto_fire_last_attempt < AUTO_FIRE_RETRY_INTERVAL:
        return

    _auto_fire_last_attempt = now

    # Direct player.shoot() is the verified working command in the client.
    # Do not route through the control handler: it can apply an additional
    # control-mode/can-shoot gate and silently prevent the actual shot.
    try:
        player.shoot()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main update loop
# ---------------------------------------------------------------------------

def _update():
    """Maintain target tracking/fire while the player's vehicle is alive."""
    player = _get_player()

    if player is None or not _is_on_arena(player):
        if _target is not None:
            _clear_target()

        _restore_shot_point_source()
        BigWorld.callback(UPDATE_INTERVAL, _update)
        return

    # Once the player's vehicle is destroyed, stop all active tracking and
    # automatic-fire work immediately. Keep only a lightweight arena poll so
    # the same mod instance can resume normally when a new vehicle appears.
    try:
        own_vehicle = player.getVehicleAttached()
        own_alive = (own_vehicle is not None and own_vehicle.isAlive())
    except Exception:
        own_alive = False

    if not own_alive:
        if _target is not None:
            _clear_target()
        else:
            _restore_shot_point_source()

        BigWorld.callback(max(UPDATE_INTERVAL, 0.25), _update)
        return

    _install_class_input_hook()
    _poll_rmb()

    if _target is not None:
        if _validate_target():
            _process_auto_fire(player)

    BigWorld.callback(UPDATE_INTERVAL, _update)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_target():
    """Return the selected vehicle or None."""
    return _target


def get_target_id():
    """Return the selected vehicle ID or None."""
    return _target_id


def get_target_point():
    """Return the current world-space target point or None."""
    return _target_point


def has_target():
    """Return True when a target is currently selected."""
    return _target is not None


def start():
    """Start the mod update loop and install the common input hook."""
    _clear_target()
    _install_class_input_hook()
    BigWorld.callback(UPDATE_INTERVAL, _update)


start()
