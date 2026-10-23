"""真实模型端到端烟测，不使用预设模型答案。"""
import json
import os
import uuid
from agent.worker import JavaClient, run_task

suffix = uuid.uuid4().hex[:8]
operator = JavaClient(os.environ.get('CONFIG_DOCTOR_OPERATOR_TOKEN','operator-token'))
worker = JavaClient()
case = operator.request('POST','/api/cases',{'id':'smoke-'+suffix,
    'symptom':'结算失败，需要定位运行证据。',
    'config':{'upstreamDelayMs':100,'timeoutMs':20,'sourcePrice':35,'cachePrice':35,'capacity':10,'taxRate':0.06}})
task = worker.request('POST','/api/tasks',{'caseId':case['id'],
    'symptom':'用户结算时出现异常，请用真实运行证据诊断。','idempotencyKey':'smoke-'+suffix})
trace = run_task(task['id'])
if trace['status'] != 'AWAITING_APPROVAL':
    raise RuntimeError(json.dumps(trace,ensure_ascii=False))
before = worker.request('GET','/api/tasks/'+task['id'])
assert before['proposal']['rootCause']=='TIMEOUT',before
assert before['proposal']['action']=='RESTORE_TIMEOUT',before
try:
    worker.request('POST',f"/api/tasks/{task['id']}/approve",{'idempotencyKey':'deny-'+suffix})
    raise AssertionError('worker approval unexpectedly allowed')
except Exception as error:
    assert getattr(error,'status',None)==403,error
approved = operator.request('POST',f"/api/tasks/{task['id']}/approve",{'idempotencyKey':'approve-'+suffix})
assert approved['verification']['passed'] is True,approved
replay = operator.request('POST',f"/api/tasks/{task['id']}/approve",{'idempotencyKey':'approve-'+suffix})
assert replay['configVersion']==approved['configVersion'],replay
result = {'taskId':task['id'],'caseId':case['id'],'status':approved['status'],
          'rootCause':before['proposal']['rootCause'],'patch':before['proposal']['patch'],
          'workerApprovalBlocked':True,'approvalReplayStable':True,
          'verification':approved['verification'],'modelResponses':trace['modelResponses'],
          'elapsedMs':trace['elapsedMs'],'toolCalls':trace['toolCalls']}
print(json.dumps(result,ensure_ascii=False,indent=2))
