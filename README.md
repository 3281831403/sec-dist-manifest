增量分发只下发变化的文件，结果客户端拿到的补丁包拼出来的版本和服务器上不一致：清单必须包含全量文件与摘要；增量包必须声明基于哪个基线版本；应用增量后必须能算出与目标版本一致的清单摘要；删除文件必须显式列出；文件路径要按相对路径规范化；摘要算法按清单声明；应用必须原子；批量应用各客户端独立。不变量：从任意基线应用到同一目标版本结果相同；重复应用同一增量结果不变。

运行方式

在仓库根目录执行：

    python -m pytest tests -q

数据约定

清单是 JSON 对象，字段为 version、algorithm、files。version 是版本标识，algorithm 是摘要算法，files 是相对路径到十六进制摘要的映射。

增量对象字段为 base_version、target_version、algorithm、add、update、delete、target_digest。add 和 update 是路径到摘要的映射，delete 是路径列表，target_digest 是目标清单按 algorithm 计算的摘要。

路径规范化后，同一路径只能出现一次；大小写或分隔符不同的写法视为同一路径，摘要冲突时清单无效。

批量应用接收客户端名到记录的映射，客户端记录包含 version 与 files。每个客户端的结果包含 ok、baseline_ok、files 与 problems。

字段

所属领域　　安全

子方向　　　软件分发完整性

任务类型　　Bugfix

语言/框架　　Python 3, pytest

环境可复现等级　无外部依赖