# 义诊预约系统 — Render 部署指南

## 你需要做的（5分钟）

### 第一步：注册 Render
打开 https://render.com → 点右上角 "Get Started" → 选 "Sign in with GitHub"

> 如果没有 GitHub 账号，先去 https://github.com 免费注册一个

### 第二步：把代码传到 GitHub
我会帮你把代码推到一个新的 GitHub 仓库，你需要授权一下。

### 第三步：在 Render 创建 Web Service
1. Render 控制台 → 点 "New +" → "Web Service"
2. 选择刚才的 GitHub 仓库
3. 填写：
   - Name: `clinicyizhen`（会变成 clinicyizhen.onrender.com）
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
4. 点 "Create Web Service"，等1分钟就部署好了

## 部署后你会得到

固定短链接：**https://clinicyizhen.onrender.com**

- 永远不变，直接在微信群里发
- 不需要开电脑，7x24小时在线
- 15分钟没人访问会休眠（下次打开等30秒自动唤醒）

## 医生密码

默认：`yizhen2026`
如需修改，在 Render 环境变量里添加 `DOCTOR_PASSWORD=新密码`

## 注意事项

- Render免费计划每月750小时，一个Web Service刚好够用
- 部署新版本时预约数据会丢失（因为存在文件里），所以**不要频繁部署**
- 如需备份，我可以帮你加一个定期导出到腾讯文档的功能
