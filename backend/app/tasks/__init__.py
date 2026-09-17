"""任务队列与登记簿。

上传的合同先登记再入队，worker 池按并发上限逐个审查；状态、闸口载荷与报告
都落在登记簿里，路由层只读登记簿。
| 模块 | 职责 |
| --- | --- |
| manager.py | TaskManager：入队、worker 池、限流退避重试 |
| store.py | 进程内登记簿（内存模式） |
| store_pg.py | Postgres 登记簿与检查点（配了 DATABASE_URL 才用） |
| uploads_admin.py | 上传目录孤儿文件清理（命令行） |

**这个包不做转出**（其它包的门面可以，这里不行）：manager 要用 review.graph，
而 review.graph 要用本包的 store——包级转出会让"先导入谁"决定成败，
直接起服务时就会撞上循环导入。用哪块就从哪个子模块显式导入。
"""
