"""业务模块路由汇总。

这里统一按别名导入再暴露 ROUTERS：模块名有可能和内置名撞车（某个业务模块就叫 dict、list
这种名字时），按名字直接 import 会把内置类型覆盖掉，函数注解在运行时求值就会报
'module' object is not subscriptable。
"""
from __future__ import annotations

from app.routers import road_section as router_road_section
from app.routers import patrol as router_patrol
from app.routers import pavement as router_pavement
from app.routers import bridge as router_bridge
from app.routers import bridge_info as router_bridge_info
from app.routers import tunnel as router_tunnel
from app.routers import traffic_facility as router_traffic_facility
from app.routers import drainage as router_drainage
from app.routers import green as router_green
from app.routers import lighting as router_lighting
from app.routers import winter as router_winter
from app.routers import flood as router_flood
from app.routers import slope as router_slope
from app.routers import expansion as router_expansion
from app.routers import replacement as router_replacement
from app.routers import bearing as router_bearing
from app.routers import project as router_project
from app.routers import vehicle as router_vehicle
from app.routers import material as router_material

ROUTERS = [router_road_section, router_patrol, router_pavement, router_bridge, router_bridge_info, router_tunnel, router_traffic_facility, router_drainage, router_green, router_lighting, router_winter, router_flood, router_slope, router_expansion, router_replacement, router_bearing, router_project, router_vehicle, router_material]
