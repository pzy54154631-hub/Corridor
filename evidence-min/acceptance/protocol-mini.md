# 六场景 HTTP 验收：意图标签说明候选 cbf824c

候选固定为cbf824c1a6b6041eb8cfe589f249e41cdcde5b3d。在dcd59b5基础上只改变分类prompt中birthday标签的说明；核心、模型客户端和业务路由未改。旧六例run、原5/6结果、严格重放装置错误及额外补偿按钮恢复证据全部保留。本协议在本次付费请求前冻结。

本runner由上一版逐字节复制，仅改候选HEAD和strict离线包装允许模式：主银行客户端仍strict；产品已有scripted财富子客户端可在network_disabled内执行；live/fallback/record在重放中均拒绝。source版本和SHA见derivation.json。不得将旧prompt记录用来证明新prompt。

固定六例沿用原协议及原终态断言，不换输入、不降标准：

1. 给妈妈转1500元：红级转账，原生MFA与最终确认，1500元原目标回执，银行增量1。
2. 看看9月账单：9月analysis receipt，settled，无设备审批、银行增量0。
3. 帮我申购2000元稳健30天持有：原生条件确认、红级卡、MFA、最终确认，P_A新增2000本金、银行增量1。
4. 我的卡丢了：原生挂失卡、确认/MFA，CARD_UI_DEMO挂失回执、银行增量1。
5. 取消订阅：明确选择页面c0且核对S1；选择不执行、设备确认才取消，银行增量1。
6. 帮我准备老婆的生日：原生4节点选择/确认，发送停止，再发送退回锁定的预算，必须通过聊天生成补偿卡并明确确认。银行增量2（锁款与解锁），原4节点compensated/cancelled/cancelled/cancelled，商户订单执行0。不得改走补偿按钮算通过。

所有操作经Browser HTTP、当前gesture/版本与模拟短信，不直接修改业务状态或回执。完整判据继承runner的run_flow，包括原对象、完整回执、设备执行增量、audit有效性与计划终态。禁止隐藏失败或重试。


每次执行保存runner/protocol副本及SHA、candidate head，live/replay须工作区干净。replay只接受相同HEAD/同执行快照的live源；六个输入完全相同、每例有journal。严格比较请求/输出哈希、业务哈希、完整回执及0新provider调用。脚本中source=live-recorded与replay分别核对，复现失败仍记失败。

这是独立合成银行HTTP集成验收，不是正式F10、SOTA评价或生产银行/真人认证。
公开入口只重放记录，不采集新响应。
