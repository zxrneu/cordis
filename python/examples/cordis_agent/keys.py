"""Share typed service declarations at the application assembly boundary."""

from cordis import ServiceKey

from .agent import AgentRuntime
from .contracts import LanguageModel
from .session import SessionLog
from .tools import ApprovalPolicy, ToolRuntime

SESSION = ServiceKey[SessionLog]("session")
POLICY = ServiceKey[ApprovalPolicy]("policy")
TOOLS = ServiceKey[ToolRuntime]("tools")
MEMORY = ServiceKey[dict[str, str]]("memory")
SYSTEM_PROMPT = ServiceKey[str]("system_prompt")
MODEL = ServiceKey[LanguageModel]("llm")
AGENT = ServiceKey[AgentRuntime]("agent")
