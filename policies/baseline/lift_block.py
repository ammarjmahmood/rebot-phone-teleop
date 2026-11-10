table = robot.task_status()
top = 0.68
found = robot.find_objects("overview", "red", z_range=(top - 0.01, top + 0.12))
if not found:
    raise RobotError("No red block seen on the table")
block = found[0]["xyz"]
print("red block seen at", block)
arm = "left" if block[1] > 0 else "right"
grasp = [block[0], block[1], top + 0.015]
robot.pick(arm, grasp, yaw_deg=0)
print("tip now", robot.tip(arm))
