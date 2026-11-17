青年培训零工与十五分钟就业圈日程规划器
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q opportunity_planner tests
验收：python3 -m opportunity_planner --database /tmp/opportunity_planner.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/opportunity_planner-output.json
单测：python3 -m unittest tests.test_domain -v

机会可达率（只读分析，不改动任何日程与承诺）
分析：python3 -m opportunity_planner reachability --fixture fixtures/reachability.json --output /tmp/reachability.json
分群：python3 -m opportunity_planner reachability --fixture fixtures/reachability.json --cohort cohort-women --person person-5 --status invalid,blocked
比较：python3 -m opportunity_planner compare-scenarios --left fixtures/reachability.json --right fixtures/reachability-alt.json --output /tmp/compare.json
复用现有排程输入：在以上命令加 --schedule，可读取 run 使用的 items/context 结构
机会组合：输入里 opportunity_mode 取 chain（默认，课程->面试->零工整链）或 independent（面试与零工各自成链）

结果口径
fulfilled    已完成且有完成时间证据，先修、两段出行、面试窗口与岗位名额均通过；按人最多计一次
theoretical  仅在理论排期上可行、没有实际完成证据；不计入已兑现安排
invalid      明确规则不成立：时间赶不上、窗口已关、名额已满或先修缺失
blocked      缺少关键行程信息（出行方向、地点、窗口或名额）；按受阻处理，不默认成功
suppressed_links 同一人另外也能满足的机会；可反查但不重复计数
capacity_lost_links 本身可行、但在分析用的名额分配中先被他人占用的机会

每条链路都带 path（课程->完成->出行->面试->换乘->岗位），failed_step 与 reasons 可反查到具体受阻环节；
read_only.inputs_unchanged 与 storage_writes=0 证明输入与现有日程、承诺未被改动。

