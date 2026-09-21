"""Authored intercepted transport; never an actual service response."""
import io,json
from test_pipeline import response
def raw(text='受控合成可见回答。',**fields):
    r=response(text,**fields);r['choices'][0]['finish_reason']='stop'
    r['usage']={'prompt_tokens':11,'completion_tokens':7,'total_tokens':18};return r

class Wire:
    def __init__(self, values=None): self.requests=[];self.values=list(values or [])
    def open(self, req, timeout):
        self.requests.append((req,json.loads(req.data) if req.data else None,timeout))
        value=self.values.pop(0) if self.values else raw('受控离线回答。')
        if callable(value):value=value(self.requests[-1][1])
        if isinstance(value,BaseException):raise value
        data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
        class R(io.BytesIO):
            status=200
            def geturl(self):return req.full_url
        return R(data)


