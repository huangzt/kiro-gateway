## 🚨 重要更新：修复 Claude Code 无限重试问题

### 问题描述
之前 Claude Code 使用 Gateway 时会出现无限重试，导致账号配额在短时间内耗尽。主要原因是错误消息不够明确，Claude Code 误判为可恢复错误。

### 本次修复
已提交 commit `5259c3b`，修复了以下问题：

#### 1. 截断错误消息改进
- **旧消息**: `[API Limitation] Your tool call was truncated... Consider adapting your approach.`
- **新消息**: `[UNRECOVERABLE ERROR] Tool call exceeded Kiro API size limits... DO NOT retry this exact operation. Retrying will fail again and waste quota.`

#### 2. 账号池超时错误改进
- 现在会明确区分三种情况：
  - 配额耗尽: `All accounts exhausted. Service unavailable until quota reset.`
  - 限流冷却: `All accounts rate-limited. Service temporarily unavailable.`
  - 全部忙碌: `All accounts busy. Service overloaded.`

#### 3. 最后账号耗尽检测
- 当最后一个可用账号配额耗尽时，立即拒绝新请求，避免无意义的排队等待

### 测试结果
✅ 所有核心功能测试通过 (65/65)

### 更新方法
```bash
git pull origin main
# 如果使用 Docker
docker-compose up -d --build
```

### 预期效果
- Claude Code 看到 `[UNRECOVERABLE ERROR]` 和 `DO NOT retry` 后应该停止重试
- 配额耗尽后会立即返回明确错误，而不是持续排队
- 减少因无限重试导致的配额浪费

### 注意事项
- 如果仍然遇到无限重试问题，请检查 Claude Code 的日志
- 建议监控账号配额消耗情况，确认修复有效

有问题请随时反馈。
