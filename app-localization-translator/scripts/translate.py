#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
APP本地化翻译主流程脚本
支持双模型交叉质检，循环优化直至通过

配置信息：
- API: Command Code Provider API (https://api.commandcode.ai/provider/v1)
- A模型(翻译): inclusionai/ling-3.0-flash-sante:free  (Ling 3.0 Flash Sante，免费额度中)
- B模型(质检): meituan/LongCat-2.0  (LongCat 2.0，付费档；:free 免费档已退役，调用返回 403)
- 输出格式: xlsx

额度提示：两个 :free 模型均为「每日每账号 100 次请求」，UTC 零点重置。
单轮运行消耗 = 1 次翻译 + N 次质检，请据此控制 --max-iterations。
"""

import os
import sys
import argparse
import pandas as pd
import requests
from typing import List, Dict

def _load_env():
    """从 .env 读配置：不引入第三方依赖，且已存在的环境变量优先。
    依次查找脚本同目录与项目根目录的 .env。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.dirname(os.path.dirname(here))):
        f = os.path.join(base, ".env")
        if os.path.isfile(f):
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# 密钥必须由环境变量提供，代码里不放任何兜底值 —— 避免随仓库提交而泄漏
API_KEY = os.environ.get("COMMANDCODE_API_KEY", "")
BASE_URL = os.environ.get("COMMANDCODE_BASE_URL", "https://api.commandcode.ai/provider/v1")


def require_key():
    """调用前显式校验，缺密钥时给出可操作的提示，而不是发出一个必然 401 的请求。"""
    if not API_KEY:
        raise RuntimeError(
            "缺少 COMMANDCODE_API_KEY。请在项目根目录创建 .env 写入 "
            "COMMANDCODE_API_KEY=<你的密钥>（.env 已被 .gitignore 排除），或先 export 该环境变量。"
        )

# 模型配置
MODEL_A = "inclusionai/ling-3.0-flash-sante:free"  # 翻译模型 Ling 3.0 Flash Sante
# 注意：:free 后缀的 LongCat 2.0 免费档已于 2026-09 退役，调用返回 403
# （"The free LongCat 2.0 tier has been retired"），必须用不带后缀的付费版
MODEL_B = "meituan/LongCat-2.0"  # 质检模型 LongCat 2.0（$0.30/$1.20 per 1M token，单次调用成本可忽略）

# LongCat 为带思维链的模型，输出预算需留足推理余量
MAX_TOKENS = 8000

# 提示词
TRANSLATION_PROMPT = """角色定位
你是出海APP小语种本地化资深翻译专家，精通各小语种互联网APP口语化本地化表达，深谙海外用户产品使用语境。

输入规则
我会依次提供：翻译目标语言 + 含「Key、中文内容」的表格。

强制输出规则
1. 严格沿用我给的表格结构，新增一列目标语言译文，最终输出完整表格，字段固定为：Key、中文内容、目标语言译文；
2. 严格逐行匹配Key，不许乱序、漏行、漏译、增译、篡改原意；
3. 译文强制通俗接地气、生活化口语化，贴合当地普通人APP日常使用口吻，拒绝书面腔、官方公文腔、文绉绉修饰、生硬直译；
4. 适配APP场景：按钮、弹窗、提示文案、功能说明、引导语等移动端UI语境；
5. 杜绝机翻感、中式翻译腔，完全遵循目标语种本土表达习惯；
6. 原文中的占位符（%1$s、%2$d、%s、{name}、\\n 等）必须与原文逐字保持一致，不得改写、增删、调整顺序；
7. 只输出规整表格，不额外解释、不闲聊、不加多余文字、不做格式外备注。"""

QA_PROMPT = """角色定位
你是出海APP小语种翻译质检终审专家，负责跨模型译文校对、本土化纠错、语义对齐、口语合规性终审。

输入内容
我会给到含「Key、中文内容、目标语言译文」的完整表格（为另一AI模型初译结果）。

强制校验 & 输出规则
1. 逐行对照：Key、中文原意、现有译文三方核验；
2. 检查维度：语义是否准确、是否符合APP本土化口语习惯、是否有语法错误、是否书面化生硬、是否漏意多意；
3. 不合理译文直接修正优化，合格译文保留不动；
4. 占位符（%1$s、%2$d、%s、{name}、\\n 等）必须与原文逐字一致，发现被改动一律改回；
4. 最终只输出定稿表格，固定字段：Key、中文内容、目标语言定稿译文；
5. 只给最终合规定稿表格，不写校验过程、不标注错误、不额外废话，直接输出双模型认可的最终标准译文版本。
6. 若所有译文全部合规、语义准确、表达地道、适配APP场景：先明确给出结论：全文翻译合规，无需修改，全部通过，再原样输出原表格"""


def call_api(model: str, system_prompt: str, user_prompt: str) -> str:
    """调用 Command Code Provider API（OpenAI 兼容）"""
    require_key()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3 if model == MODEL_A else 0.2,
        "max_tokens": MAX_TOKENS,
        "stream": False
    }
    response = requests.post(f"{BASE_URL}/chat/completions", headers=headers, json=data, timeout=600)
    result = response.json()
    if 'choices' not in result:
        raise RuntimeError(f"接口返回异常 [{response.status_code}]：{result}")
    choice = result['choices'][0]
    if choice.get('finish_reason') == 'length':
        print(f"   ⚠️ 输出被截断（{model}），请提高 MAX_TOKENS 或减少单次翻译行数")
    # 思维链模型（如 LongCat）的正文在 content 字段，可能为空
    return choice['message'].get('content') or ''


def parse_table(content: str) -> Dict[str, str]:
    """解析表格内容，返回key到译文的映射"""
    mapping = {}
    for line in content.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('-') or 'Key' in line:
            continue
        if line.startswith('|'): line = line[1:]
        if line.endswith('|'): line = line[:-1]
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 3:
            key = parts[0]
            if key and key not in ['nan', '---']:
                mapping[key] = parts[2]
    return mapping


def read_input(input_path: str) -> List[Dict[str, str]]:
    """读取输入文件（支持xlsx和csv）"""
    if input_path.endswith('.xlsx'):
        df = pd.read_excel(input_path)
    else:
        df = pd.read_csv(input_path, encoding='utf-8-sig')
    
    # 自动检测列名
    key_col = 'key' if 'key' in df.columns else df.columns[0]
    zh_col = 'zh' if 'zh' in df.columns else df.columns[1]
    
    items = []
    for _, row in df.iterrows():
        items.append({'key': str(row[key_col]), 'zh': str(row[zh_col])})
    return items


def run_translation(input_path: str, target_lang: str, output_path: str, max_iterations: int = 3):
    """执行翻译流程"""
    print(f"\n{'='*60}")
    print(f"🚀 APP本地化翻译")
    print(f"{'='*60}")
    print(f"📁 输入文件: {input_path}")
    print(f"🌐 目标语言: {target_lang}")
    print(f"🤖 翻译模型A: {MODEL_A}")
    print(f"🔍 质检模型B: {MODEL_B}")
    print(f"{'='*60}\n")
    
    # 读取输入
    items = read_input(input_path)
    print(f"📖 读取 {len(items)} 条待翻译内容\n")
    
    # 构建翻译表格
    lines = ["Key|中文内容"]
    for item in items:
        lines.append(f"{item['key']}|{item['zh']}")
    table_content = "\n".join(lines)
    
    # 步骤1: 翻译
    print(f"📝 步骤1: 翻译模型A ({MODEL_A})...")
    user_prompt = f"目标语言：{target_lang}\n\n{table_content}"
    translate_result = call_api(MODEL_A, TRANSLATION_PROMPT, user_prompt)
    translation_map = parse_table(translate_result)
    print(f"   ✅ 翻译完成，共 {len(translation_map)} 条\n")
    
    # 步骤2: 质检循环
    current_map = translation_map
    for iteration in range(1, max_iterations + 1):
        print(f"🔍 步骤2.{iteration}: 质检模型B ({MODEL_B})...")
        
        # 构建质检表格
        qa_lines = [f"Key|中文内容|{target_lang}译文"]
        for item in items:
            qa_lines.append(f"{item['key']}|{item['zh']}|{current_map.get(item['key'], '')}")
        qa_table = "\n".join(qa_lines)
        
        qa_result = call_api(MODEL_B, QA_PROMPT, f"请对以下翻译结果进行质检：\n\n{qa_table}")
        qa_map = parse_table(qa_result)
        
        # 空结果说明正文被推理过程吃光或未按要求输出表格，保留上一版译文
        if not qa_map:
            print(f"   ⚠️ 未解析到质检表格，保留现有译文\n")
            break
        
        # 检查是否通过
        if '全文翻译合规' in qa_result or '全部通过' in qa_result or '无需修改' in qa_result:
            print(f"   ✅ 质检通过！\n")
            current_map = qa_map
            break
        else:
            print(f"   ⚠️ 发现问题，优化中...\n")
            current_map = qa_map
    
    # 构建最终结果
    final_results = []
    for item in items:
        final_results.append({
            'key': item['key'],
            'zh': item['zh'],
            'id': current_map.get(item['key'], '')
        })
    
    # 保存为xlsx
    output_df = pd.DataFrame(final_results)
    if not output_path.endswith('.xlsx'):
        output_path += '.xlsx'
    output_df.to_excel(output_path, index=False, engine='openpyxl')
    
    print(f"{'='*60}")
    print(f"✅ 翻译完成！")
    print(f"{'='*60}")
    print(f"📁 输出文件: {output_path}")
    print(f"📊 共 {len(final_results)} 条\n")
    
    return output_path


def main():
    parser = argparse.ArgumentParser(description="APP本地化翻译工具 - 双模型交叉质检")
    parser.add_argument("--input", "-i", required=True, help="输入文件路径 (xlsx/csv)")
    parser.add_argument("--target-lang", "-t", required=True, help="目标语言")
    parser.add_argument("--output", "-o", default=None, help="输出文件路径 (默认xlsx格式)")
    parser.add_argument("--max-iterations", "-m", type=int, default=3, help="最大质检循环次数")
    
    args = parser.parse_args()
    
    if args.output is None:
        base_name = os.path.splitext(os.path.basename(args.input))[0]
        args.output = f"{base_name}_{args.target_lang}_定稿.xlsx"
    
    run_translation(args.input, args.target_lang, args.output, args.max_iterations)


if __name__ == "__main__":
    main()
