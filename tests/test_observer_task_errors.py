import json
from types import SimpleNamespace
import pytest
from cachemonitor.observer_task import ObserverTask,TaskSchedulerError

@pytest.mark.parametrize('stage,code',[('connect','0x80070005'),('root-folder','0x80070003'),('lookup-task','0x80070005'),('register','0x80070005')])
def test_scheduler_failure_preserves_context(monkeypatch,stage,code):
    monkeypatch.setattr('cachemonitor.observer_task.subprocess.run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=json.dumps({'error':{'stage':stage,'hresult':code}}).encode(),stderr=b'private diagnostics'))
    with pytest.raises(TaskSchedulerError) as caught:ObserverTask('synthetic').inspect()
    assert isinstance(caught.value,RuntimeError)
    assert (caught.value.operation,caught.value.stage,caught.value.hresult)==('inspect',stage,code)
    assert 'private' not in str(caught.value)
