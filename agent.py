import json
import re
from status_tools import SCHEMAS

SYSTEM_PROMPT = '''TUYỆT ĐỐI KHÔNG trả lời dựa trên kiến thức nội tại hoặc phỏng đoán. BẠN BẮT BUỘC PHẢI sử dụng công cụ (tools) theo đúng quy trình 2 bước sau trước khi đưa ra câu trả lời cuối cùng:
1. LUÔN sử dụng công cụ `get_status` đầu tiên để kiểm tra danh sách sự cố và lấy ID (incident_id) của sự cố liên quan.
2. NẾU tìm thấy sự cố, BẠN BẮT BUỘC PHẢI tiếp tục sử dụng công cụ `get_incident_details` với ID vừa tìm được để đọc báo cáo chi tiết.
3. Khi trả lời về sự cố cụ thể, BẮT BUỘC phải đính kèm URL nguồn (shortlink/url từ kết quả công cụ) vào câu trả lời cuối cùng.
Chỉ đưa ra câu trả lời cuối cùng cho người dùng SAU KHI đã gọi công cụ. Trả về đúng định dạng JSON khi gọi tool, không kèm giải thích thừa.'''


def run_agent(question, provider, tools, max_steps=6):
    """Trả dict: status, answer, trace, usage. Contract chi tiết trong GUIDE.md."""
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question}
    ]
    trace = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    
    valid_tool_names = []
    for schema in SCHEMAS:
        if "function" in schema:
            valid_tool_names.append(schema["function"].get("name"))
        else:
            valid_tool_names.append(schema.get("name"))
            
    # Danh sách lưu các URL bằng chứng thu được từ get_incident_details thành công
    evidence_urls = []

    for step in range(max_steps):
        try:
            response = provider.complete(messages, SCHEMAS)
        except Exception as e:
            return {
                "status": "error",
                "answer": "",
                "trace": trace,
                "usage": usage,
                "error": str(e)
            }
        
        message = response.get("message", {})
        calls = response.get("calls", [])
        finish = response.get("finish")
        step_usage = response.get("usage", {})
        
        for key in usage:
            usage[key] += step_usage.get(key, 0)
            
        messages.append(message)
        
        trace.append({
            "event": "model_reply",
            "step": step + 1,
            "message": message,
            "calls": calls,
            "finish": finish
        })
        
        if finish == 'length':
            return {
                "status": "error",
                "answer": message.get("content", ""),
                "trace": trace,
                "usage": usage,
                "error": "Model output truncated (length)"
            }
        
        if calls:
            for call in calls:
                call_id = call.get("id")
                if "function" in call:
                    name = call["function"].get("name")
                    args_str = call["function"].get("arguments", "{}")
                else:
                    name = call.get("name")
                    args_str = call.get("arguments", "{}")
                
                try:
                    if not isinstance(args_str, str):
                        raise TypeError("Arguments must be a JSON string")
                    args = json.loads(args_str)
                    if not isinstance(args, dict):
                        raise ValueError("Arguments must be a JSON object")
                        
                    if name not in valid_tool_names:
                        result = {"error": f"Tool {name} does not exist"}
                    else:
                        result = tools.execute(name, args)
                except (json.JSONDecodeError, TypeError, ValueError):
                    result = {"error": "INVALID_ARGUMENTS"}
                except Exception as e:
                    result = {"error": str(e)}
                
                # Nếu gọi get_incident_details thành công (không có lỗi), thu thập URL bằng chứng
                if name == "get_incident_details" and isinstance(result, dict) and "error" not in result:
                    urls_in_result = re.findall(r'https?://[^\s"\'<>]+', json.dumps(result))
                    evidence_urls.extend(urls_in_result)
                    
                result_str = json.dumps(result, ensure_ascii=False)
                
                trace.append({
                    "event": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "tool_result": result_str
                })
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result_str
                })
            
            continue 
            
        else:
            answer = message.get("content", "")
            
            if finish == 'stop':
                # Kiểm tra Evidence Gate: nếu có URL bằng chứng từ get_incident_details mà answer chưa trích dẫn
                if evidence_urls and not any(u in answer for u in evidence_urls):
                    trace.append({
                        "event": "validation_missing_source"
                    })
                    messages.append({
                        "role": "user",
                        "content": f"Câu trả lời thiếu URL nguồn. Vui lòng bổ sung URL bằng chứng ({evidence_urls[0]}) vào câu trả lời."
                    })
                    continue
                
                return {
                    "status": "completed",
                    "answer": answer,
                    "trace": trace,
                    "usage": usage
                }
            else:
                return {
                    "status": "error",
                    "answer": answer,
                    "trace": trace,
                    "usage": usage
                }
                
    final_answer = messages[-1].get("content", "") if messages else ""
    return {
        "status": "max_steps",
        "answer": final_answer,
        "trace": trace,
        "usage": usage
    }