"""Combination regressions; only synthetic inputs and isolated SQLite stores."""
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
import io, sqlite3, tempfile, unittest
from unittest.mock import patch
from steady_companion.engine import Conversation
from steady_companion.store import Store, StaleRevisionError
from steady_companion.cli import execute_command

class Fake:
 def complete(self,messages,**kw):
  self.messages=deepcopy(messages)
  return {'choices':[{'message':{'role':'assistant','content':'[offline fixture]'}}]}

class CombinedTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.store=Store(Path(t.name));self.restart();self.agent.natural_memory.set_mode('auto')
 def restart(self): self.fake=Fake();self.agent=Conversation(self.fake,self.store,response_mode='direct')
 def rows(self): return self.agent.natural_memory.list()
 def say(self,t): return self.agent.reply(t)
 def fail(self,t):
  with patch.object(self.agent.natural_memory,'save_current',side_effect=sqlite3.OperationalError('synthetic')): return self.say(t)
 def command(self,t):
  with redirect_stdout(io.StringIO()): execute_command(t,self.agent,self.store)
 def test_scope_after_day_exception_and_restart(self):
  for topic in ('成绩','游戏','考试'):
   with self.subTest(topic=topic):
    self.command('/auto-forget all');self.say('以后别开玩笑。');self.say('今天可以，平时还是别说。');self.say('以后别每句都问问题。')
    t=self.say(f'以后只在聊{topic}时别开玩笑，其他话题可以。')
    self.assertEqual(t.memory_update['status'],'scope_changed');self.restart();self.say('聊聊电影')
    self.assertEqual({r['semantic_key'] for r in self.rows()},{f'boundary:joke:{topic}','boundary:questions:every_turn'})
    self.assertNotIn('以后别开玩笑。',str(self.fake.messages))
 def test_scope_after_turn_exception(self):
  self.say('以后别开玩笑。');self.say('这次可以开玩笑。');self.say('以后只在聊成绩时别开玩笑，其他话题可以。')
  self.restart();self.assertEqual([r['semantic_key'] for r in self.rows()],['boundary:joke:成绩'])
 def test_scope_transaction_failure_rolls_back_deletion_and_insert(self):
  self.say('以后别开玩笑。');self.say('今天可以，平时还是别说。');before=self.rows()
  with self.store._connection() as c: c.execute("CREATE TRIGGER fail_scope BEFORE INSERT ON p1_memories BEGIN SELECT RAISE(ABORT,'synthetic'); END")
  t=self.say('以后只在聊成绩时别开玩笑，其他话题可以。')
  self.assertEqual(t.memory_update['status'],'not_saved');self.assertEqual(self.rows(),before)
  self.say('成绩');self.assertNotIn('以后别开玩笑。',str(self.fake.messages))
  self.restart();self.assertEqual(self.rows(),before)
 def test_explicit_repeat_after_failed_correction_commits_once(self):
  for topic in ('篮球','游泳','钢琴'):
   self.say(f'我喜欢{topic}。');self.fail(f'我不喜欢{topic}。')
   t=self.say(f'我不喜欢{topic}。');self.assertEqual(t.memory_update['status'],'corrected')
   rev=self.store.revision();self.assertEqual(self.say(f'我不喜欢{topic}。').memory_update['status'],'duplicate');self.assertEqual(self.store.revision(),rev)
  self.restart();self.assertEqual(len(self.rows()),3);self.assertTrue(all(r['meaning'].startswith('negative') for r in self.rows()))
 def test_no_input_no_retry_and_delete_clears_failed_candidate(self):
  self.say('我喜欢篮球。');self.fail('我不喜欢篮球。')
  with patch.object(self.agent.natural_memory,'save_current',side_effect=AssertionError('no retry')):
   self.say('你好');self.command('/auto-forget A1');self.say('篮球')
  self.assertEqual(self.rows(),[]);self.assertNotIn('我不喜欢篮球',str(self.fake.messages));self.assertFalse(self.agent._p1_context)
 def test_other_process_revision_discards_failed_candidate(self):
  self.say('我喜欢篮球。');self.fail('我不喜欢篮球。')
  other=Conversation(Fake(),Store(self.store.data_dir),response_mode='direct');other.natural_memory.forget(1)
  self.say('篮球');self.assertEqual(self.rows(),[]);self.assertFalse(self.agent._memory_overrides)
 def test_cancelled_generation_does_not_leave_pending_commit(self):
  self.say('我喜欢篮球。')
  with patch.object(self.fake,'complete',side_effect=KeyboardInterrupt):
   with self.assertRaises(KeyboardInterrupt):self.say('我不喜欢篮球。')
  self.say('你好');self.restart();self.assertEqual(self.rows()[0]['meaning'],'positive篮球')
