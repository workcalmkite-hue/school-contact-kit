import json
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import api
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

server = Server("naver-mail")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_recent_naver_mails",
            description="네이버 메일 최근 목록을 가져옵니다.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "가져올 메일 수 (기본 10)", "default": 10},
                    "mailbox": {"type": "string", "description": "메일함 (기본 INBOX)", "default": "INBOX"},
                },
            },
        ),
        types.Tool(
            name="search_naver_mails",
            description="네이버 메일함에서 제목/발신자/본문 일부로 검색합니다.",
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "검색어"},
                    "limit": {"type": "integer", "description": "가져올 결과 수 (기본 10)", "default": 10},
                    "mailbox": {"type": "string", "description": "메일함 (기본 INBOX)", "default": "INBOX"},
                },
            },
        ),
        types.Tool(
            name="get_naver_mail",
            description="특정 네이버 메일의 전체 본문을 가져옵니다. 첨부파일 목록(파일명)도 함께 반환됩니다.",
            inputSchema={
                "type": "object",
                "required": ["uid"],
                "properties": {
                    "uid": {"type": "string", "description": "메일 UID (list/search 결과의 uid 값)"},
                    "mailbox": {"type": "string", "description": "메일함 (기본 INBOX)", "default": "INBOX"},
                },
            },
        ),
        types.Tool(
            name="list_naver_mail_attachments",
            description="특정 네이버 메일의 첨부파일 목록(파일명/타입/크기)을 가져옵니다.",
            inputSchema={
                "type": "object",
                "required": ["uid"],
                "properties": {
                    "uid": {"type": "string", "description": "메일 UID (list/search 결과의 uid 값)"},
                    "mailbox": {"type": "string", "description": "메일함 (기본 INBOX)", "default": "INBOX"},
                },
            },
        ),
        types.Tool(
            name="download_naver_mail_attachment",
            description="특정 네이버 메일의 첨부파일을 로컬(mcp/naver-mail/downloads/)에 다운로드하고 저장 경로를 반환합니다.",
            inputSchema={
                "type": "object",
                "required": ["uid", "filename"],
                "properties": {
                    "uid": {"type": "string", "description": "메일 UID (list/search 결과의 uid 값)"},
                    "filename": {"type": "string", "description": "다운로드할 첨부파일명 (list_naver_mail_attachments 결과의 filename 값)"},
                    "mailbox": {"type": "string", "description": "메일함 (기본 INBOX)", "default": "INBOX"},
                },
            },
        ),
        types.Tool(
            name="send_naver_mail",
            description="내 네이버 메일 계정으로 메일을 실제 발송합니다. 사용자가 받는 사람·제목·본문을 확인한 뒤에만 호출합니다.",
            inputSchema={
                "type": "object",
                "required": ["to", "subject", "body"],
                "properties": {
                    "to": {"type": "string", "description": "받는 사람 이메일 (콤마로 여러 명 가능)"},
                    "subject": {"type": "string", "description": "제목"},
                    "body": {"type": "string", "description": "본문 (일반 텍스트)"},
                    "cc": {"type": "string", "description": "참조 이메일 (콤마로 여러 명 가능, 선택)"},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "첨부할 파일의 절대경로 목록 (선택)",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "list_recent_naver_mails":
        mails = api.list_recent_mails(
            limit=arguments.get("limit", 10),
            mailbox=arguments.get("mailbox", "INBOX"),
        )
        return [types.TextContent(type="text", text=json.dumps(mails, ensure_ascii=False, indent=2))]

    if name == "search_naver_mails":
        mails = api.search_mails(
            arguments["query"],
            limit=arguments.get("limit", 10),
            mailbox=arguments.get("mailbox", "INBOX"),
        )
        return [types.TextContent(type="text", text=json.dumps(mails, ensure_ascii=False, indent=2))]

    if name == "get_naver_mail":
        mail = api.get_mail(arguments["uid"], mailbox=arguments.get("mailbox", "INBOX"))
        return [types.TextContent(type="text", text=json.dumps(mail, ensure_ascii=False, indent=2))]

    if name == "list_naver_mail_attachments":
        attachments = api.list_attachments(arguments["uid"], mailbox=arguments.get("mailbox", "INBOX"))
        return [types.TextContent(type="text", text=json.dumps(attachments, ensure_ascii=False, indent=2))]

    if name == "download_naver_mail_attachment":
        path = api.download_attachment(
            arguments["uid"],
            arguments["filename"],
            mailbox=arguments.get("mailbox", "INBOX"),
        )
        return [types.TextContent(type="text", text=json.dumps({"path": path}, ensure_ascii=False))]

    if name == "send_naver_mail":
        result = api.send_mail(
            arguments["to"],
            arguments["subject"],
            arguments["body"],
            cc=arguments.get("cc"),
            attachments=arguments.get("attachments"),
        )
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
