"""ccs task 的执行体：启动/复用 scout，并把任务描述投递给它。"""


def run_task(description: str) -> int:
    from core import send, start

    print(f"已接收任务：{description}")
    print("执行计划：scout -> engineer -> reviewer -> closer")

    start_result = start("scout", detach=True)
    if start_result.get("success") is not True:
        print(f"启动 scout 失败：{start_result.get('error') or start_result}")
        return 1

    send_result = send("scout", description, source="cli")
    if send_result.get("success") is not True:
        print(f"投递任务失败：{send_result.get('error') or send_result}")
        return 1

    print("scout 已接收任务")
    return 0
