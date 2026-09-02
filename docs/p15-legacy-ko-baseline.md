# P15 旧 KO 迁移基线

- 生成时间：2026-08-20T16:03:37
- 备份路径：`C:\Users\20474\Documents\学习Agent\gitlab-rag-feature\backend\data\backups\notes.db.p15-20260820-160336.bak`
- SHA-256：`17e5357f2d6420c5859273d0befcec289303c44f6a6672d496b2e861c620c612`
- 大小：160096256 bytes

## 状态分布

- `extraction_failed`：35
- `pending`：358
- `published`：9
- `rejected`：1

## published KO 映射

- 总数：9
- 已映射到 Published Card：6
- 需重编译：3

- `b28c221b-0452-4721-a492-964b67245bc4` 本页重要业务图片 → card=`178e4e40-4e02-4a2a-8eba-aa35eba35b0e` status=`published` evidence=84 verdict=`migrated`
- `d39efa89-1727-4a72-994b-729765da9175` 部署步骤 → card=`db166cde-fe02-4022-8f1f-095f6f2170d9` status=`published` evidence=103 verdict=`migrated`
- `c09b4579-73f4-405b-bbdd-d874f9cddbb0` 第 1 页 - SKYWALKER 50 → card=`1ca44c8e-f028-4153-abc1-858c95f565d4` status=`published` evidence=103 verdict=`migrated`
- `975c11a5-d8a5-40f1-ac43-ac132ccc152d` 本页重要业务图片 → card=`0161706b-2284-4f47-b808-9acec48a6d50` status=`published` evidence=103 verdict=`migrated`
- `16981806-78fa-47b8-b78a-fe94790a011d` 本页图片文字（OCR） → card=`922d451a-91f2-4eb2-bbee-8b1ae31560b8` status=`published` evidence=103 verdict=`migrated`
- `a452b5a4-fe1a-443b-97c4-fc5c2cb01277` 第 2 页 - Description → card=`cfeab2fe-6554-4cb5-9741-e98dfd99089b` status=`published` evidence=103 verdict=`migrated`
- `fe198e87-58a0-4946-9b4c-fd85512cea18` Titan 810 驱动最低要求 → card=`f2a7e1e5-cac2-446e-85d3-63fcd5e78093` status=`draft` evidence=0 verdict=`needs_recompile`
- `1a487222-3332-40d2-9472-d2b71eb46a64` Titan 810 驱动最低要求 → card=`10f77c7b-951f-43fd-8eb8-5978be03f956` status=`draft` evidence=0 verdict=`needs_recompile`
- `43dd47cb-311c-4639-9a00-df1cddb96096` Titan 810 标准电池容量 → card=`6c9207ab-4551-40c7-a087-424c35063888` status=`draft` evidence=0 verdict=`needs_recompile`

## pending KO

- 总数：358
- 来源 Page 数：5
- 缺少 source_page_id：0

不逐条审核，按 source_page_id 回到 Page/Evidence 重新编译 Card Proposal。

## extraction_failed

- 总数：35

- `5386bd69-f372-41c9-81ef-dc034e2aceb2` 编译失败：(无标题)（可重编译）：生成失败：LLM 返回为空或调用失败
- `67e15862-2bb9-4f2d-8286-37d905a9195a` 编译失败：第 1 页 - ⼯作站OTA升级 - 1（可重编译）：生成失败：LLM 返回为空或调用失败
- `b18cd3c2-90ef-4db8-b8cc-f53012d75745` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `f2df038c-c5fb-4314-8f8e-0b8246ce0a60` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `0065e557-8ddd-4331-b186-920cecf97342` 编译失败：第 2 页 - ⼯作站OTA升级 - 2（可重编译）：生成失败：LLM 返回为空或调用失败
- `a3d68d7e-a726-470a-8da4-ccee9ddf8d69` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `be666da6-c5b0-4061-b6ab-8cf980eec142` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `b5f9b55d-7ea5-45a5-a6f0-8f80624cdc76` 编译失败：第 3 页 - ⼯作站OTA升级 - 3（可重编译）：生成失败：LLM 返回为空或调用失败
- `803a9fdc-552c-44fd-90f8-84220d02ad1e` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `92436ba5-7e70-46d6-b546-6c06665884a6` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `2b298f2f-f569-42d0-a52e-fedd4157a1c4` 编译失败：第 4 页 - 9、等待升级结束（可重编译）：生成失败：LLM 返回为空或调用失败
- `36875f81-70e5-4033-ac65-26f20d1684a4` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `b470a0cd-74d8-46eb-a8e5-55f95485e326` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `37057263-d5a4-4305-8ca2-c41011ab73da` 编译失败：第 5 页 - 2、复制固件地址（可重编译）：生成失败：LLM 返回为空或调用失败
- `a4aa4278-c3cc-4749-87fa-25e3a3d6c66b` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `a60c4f1c-b61b-4626-a65d-300ab5c5e442` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `59882ba8-33d6-4a7d-823b-0586db078260` 编译失败：第 6 页 - 3、通过公司 ops 远程到机器上（可重编译）：生成失败：LLM 返回为空或调用失败
- `7639d062-14ae-4b88-8b2a-bdb3f5a3d333` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `5602226d-ffeb-4538-acc0-93e0d4655b8a` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `810c15ba-3ed7-430b-91f4-c83f0aa327f7` 编译失败：第 7 页 - 5、wget 下载固件（可重编译）：生成失败：LLM 返回为空或调用失败
- `ba1258ab-087e-4908-8ebf-e5d24e9753d3` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `1ae9ff2c-2f4f-4791-88e0-11470ee176fa` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `838e76df-4d3f-4b11-84a4-69faa5c89a60` 编译失败：第 8 页 - ⼯作站OTA升级 - 8（可重编译）：生成失败：LLM 返回为空或调用失败
- `778d60a1-b909-4659-a69e-1c43578e4f61` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `25e213df-0342-4c3b-a8e3-e97d065578fc` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `744b3db4-c453-4b64-b0df-f9ae55e5e17a` 编译失败：第 9 页 - ⼯作站OTA升级 - 9（可重编译）：生成失败：LLM 返回为空或调用失败
- `c5ce7021-c5f1-4055-9ac4-50d78dc5ac70` 编译失败：本页重要业务图片（可重编译）：生成失败：LLM 返回为空或调用失败
- `05e5b110-a192-4902-8f40-5fcbc508e169` 编译失败：本页图片文字（OCR）（可重编译）：生成失败：LLM 返回为空或调用失败
- `ca31dff1-7909-4a8b-9a16-93eb6b703514` 编译失败：(无标题)（可重编译）：生成失败：LLM 返回为空或调用失败
- `4e689326-1f16-44c4-90f6-2a16daba5cbc` 编译失败：塑料dock改造套件：（可重编译）：生成失败：LLM 返回为空或调用失败
- `2b17edb3-40f0-411f-99c7-b03503f91544` 编译失败：工具：（可重编译）：生成失败：LLM 返回为空或调用失败
- `5d2525c6-203d-4aa2-828e-04b4ec68348c` 编译失败：(无标题)（可重编译）：生成失败：LLM 返回为空或调用失败
- `f8e02fdb-afa3-4516-b215-d04595350bb1` 编译失败：(无标题)（可重编译）：生成失败：LLM 返回为空或调用失败
- `f01d7705-49a7-48c9-aa38-e83980bcb5c5` 编译失败：(无标题)（可重编译）：生成失败：LLM 返回为空或调用失败
- `927ec7ff-53b8-4c3c-8129-0a8c8b572dd1` 编译失败：(无标题)（可重编译）：生成失败：LLM 返回为空或调用失败

## 按 Page 重编译 Card Proposal

- 成功：9
- 跳过：0
- 失败：0
