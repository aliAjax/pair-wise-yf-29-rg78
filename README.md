# 法律证据保管与流转后台

仅使用 Python 3.11+ 标准库实现的证据保管项目。支持真实 SHA-256 入册、封存/开箱/移交、分析衍生关系、案件成员权限、法律保留、保留期限、不可变保管事件链和 JSON 报告导出。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8105>，默认数据库 `custody.db`。测试：

```bash
python3 -m unittest -v
```

演示身份：`custodian1`、`custodian2`、`analyst1`、`auditor1`、`outsider`。请求使用 `X-User-Id`。

## 主要接口

- `POST /api/cases`：创建案件，创建人自动成为保管员。
- `POST /api/cases/{id}/members`：授予 custodian、analyst 或 auditor 角色。
- `POST /api/cases/{id}/evidence`：以 Base64 入册证据，服务端计算 SHA-256 和大小。
- `GET /api/evidence/{id}`：查看元数据、完整保管事件链、当前链尾、完整性结果和衍生关系。
- `POST /api/evidence/{id}/open`：保管员开箱。
- `POST /api/evidence/{id}/transfer`：移交保管人并记录位置。
- `POST /api/evidence/{id}/derive`：分析员从已开箱证据创建衍生证据。
- `POST /api/evidence/{id}/hold`：审计员或案件创建人设置/解除法律保留。
- `POST /api/evidence/{id}/retention`：变更保留期限；未释放证据随即进入“待重新确认保管结论”状态。
- `POST /api/evidence/{id}/reconfirm`：保管员重新确认保管结论，解除该状态。
- `POST /api/evidence/{id}/release`：存在法律保留或待重新确认时拒绝；需两名保管员分别见证（两次独立提交、接收方一致、不得同人重复），见证齐全后才执行释放。
- `GET /api/cases/{id}/report`：校验所有证据哈希和每条事件链，导出完整报告。
- 所有 `DELETE` 请求返回 405；证据和保管记录不提供删除接口。

## 链尾并发控制

所有会写入保管链的提交（开箱、移交、分析、法律保留、保留期限、重新确认、释放见证）都必须携带 `expected_tail`，即提交前在证据详情中看到的 `chain_tail`。`GET /api/evidence/{id}` 返回 `chain_tail`/`chain_length`，页面也会直接显示当前链尾，操作表单自动携带。

- 链位已被他人占用时返回 409 `chain_conflict`，需重新查看最新链尾后再提交。
- 写盘失败时事务整体回滚、不占用链位，用原 `expected_tail` 重试即可接着原链位写入。
- 缺少 `expected_tail` 返回 422 `missing_chain_tail`。

## 报告链核对

报告按链逐条核对：序号连续性、前序哈希衔接和事件哈希重算。发现断链（`broken_chain`）、同一保管员重复见证（`duplicate_witness`）或释放缺少双人见证（`release_witness_shortage`）时，在 `chain_issues` 中点名证据编号与标签，并将 `overall_integrity_valid` 置为 false。

保管事件通过前一条事件哈希串联；报告会重新计算文件哈希和事件链。项目适合流程与完整性原型，不涵盖现实中的签名证书、WORM 存储、证据文件加密或司法辖区合规认证。
