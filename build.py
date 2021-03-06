#!/usr/bin/env python3

import zipfile
import json
import time
import threading
import queue
import shutil
import subprocess
import multiprocessing
import sys
import os
import urllib.request

deps = [
  ('org.codehaus.jackson', 'jackson-core-asl', '1.9.13'),
  ('org.codehaus.jackson', 'jackson-mapper-asl', '1.9.13'),
  ('commons-codec', 'commons-codec', '1.10'),
  ('net.minidev', 'json-smart', '1.2')
  ]

testDeps = [
  ('com.carrotsearch.randomizedtesting', 'junit4-ant', '2.0.13'),
  ('com.carrotsearch.randomizedtesting', 'randomizedtesting-runner', '2.3.4'),
  ('junit', 'junit', '4.10')
  ]
  
LUCENE_VERSION = '6.2.0-SNAPSHOT'
LUCENE_SERVER_VERSION = '0.1.0-SNAPSHOT'

luceneDeps = ('core',
              'analyzers-common',
              'analyzers-icu',
              'facet',
              'codecs',
              'grouping',
              'highlighter',
              'join',
              'misc',
              'queries',
              'queryparser',
              'suggest',
              'expressions',
              'replicator',
              'sandbox')

luceneTestDeps = ('test-framework',)

TEST_HEAP = '512m'

printLock = threading.Lock()

def message(s):
  with printLock:
    print(s)

def unescape(s):
  return s.replace('%0A', '\n').replace('%09', '\t')

class RunTestsJVM(threading.Thread):

  def __init__(self, id, jobs, classPath, verbose, seed, doPrintOutput, testMethod):
    threading.Thread.__init__(self)
    self.id = id
    self.jobs = jobs
    self.classPath = classPath
    self.verbose = verbose
    self.seed = seed
    self.testMethod = testMethod
    self.testCount = 0
    self.suiteCount = 0
    self.failCount = 0
    self.doPrintOutput = doPrintOutput

  def run(self):
    cmd = ['java']
    cmd.append('-Xmx%s' % TEST_HEAP)
    cmd.append('-cp')
    cmd.append(':'.join(self.classPath))

    if self.verbose:
      cmd.append('-Dtests.verbose=true')
    cmd.append('-Djava.util.logging.config=lib/logging.properties')
    cmd.append('-DtempDir=build/temp')
    if self.seed is not None:
      cmd.append('-Dtests.seed=%s' % self.seed)

    if self.testMethod is not None:
      cmd.append('-Dtests.method=%s' % self.testMethod)

    if self.verbose:
      cmd.append('-Dtests.verbose=true')

    cmd.append('-ea')
    cmd.append('-esa')
    cmd.append('com.carrotsearch.ant.tasks.junit4.slave.SlaveMainSafe')

    eventsFile = 'build/test/%d.events' % self.id
    if os.path.exists(eventsFile):
      os.remove(eventsFile)
    cmd.append('-eventsfile')
    cmd.append(eventsFile)

    cmd.append('-flush')
    cmd.append('-stdin')

    #print('COMMAND: %s' % ' '.join(cmd))

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    events = ReadEvents(p, eventsFile)
    events.waitIdle()

    while True:
      job = self.jobs.get()
      if job is None:
        break

      self.suiteCount += 1

      message('%s...' % job[25:])

      p.stdin.write((job + '\n').encode('utf-8'))
      p.stdin.flush()
      lines = []

      pendingOutput = []

      didFail = False
      testCaseFailed = False
      testCaseName = None
      
      while True:
        l = events.readline()
        l = l.rstrip()
        if l == ']':
          lines.append(l)
          #print('DECODE:\n%s' % '\n'.join(lines))
          if lines[1] == '[':
            #print('  DO LOOK')
            event = json.loads('\n'.join(lines))
            if event[0] in ('APPEND_STDOUT', 'APPEND_STDERR'):
              chunk = unescape(event[1]['chunk'])
              if testCaseFailed or self.doPrintOutput:
                message(chunk)
              else:
                pendingOutput.append(chunk)
            elif event[0] in ('TEST_FAILURE', 'SUITE_FAILURE'):
              details = event[1]['failure']
              s = '\n!! %s.%s FAILED !!:\n\n' % (job[25:], testCaseName)
              s += ''.join(pendingOutput)
              if 'message' in details:
                s += '\n%s\n' % details['message']
              s += '\n%s' % details['trace']
              message(s)
              testCaseFailed = True
              if not didFail:
                self.failCount += 1
                didFail = True
            elif event[0] == 'TEST_STARTED':
              self.testCount += 1
              testCaseFailed = False
              testCaseName = event[1]['description']
              i = testCaseName.find('#')
              j = testCaseName.find('(')
              testCaseName = testCaseName[i+1:j]
            elif event[0] == 'IDLE':
              break
          lines = []
        else:
          lines.append(l)
        
class ReadEvents:

  def __init__(self, process, fileName):
    self.process = process
    self.fileName = fileName
    while True:
      try:
        self.f = open(self.fileName, 'rb')
      except IOError:
        time.sleep(.01)
      else:
        break
    self.f.seek(0)
    
  def readline(self):
    while True:
      pos = self.f.tell()
      l = self.f.readline().decode('utf-8')
      if l == '' or not l.endswith('\n'):
        time.sleep(.01)
        p = self.process.poll()
        if p is not None:
          raise RuntimeError('process exited with status %s' % str(p))
        self.f.seek(pos)
      else:
        return l

  def waitIdle(self):
    lines = []
    while True:
      l = self.readline()
      if l.find('"IDLE",') != -1:
        return lines
      else:
        lines.append(l)

def fetchMavenJAR(org, name, version, destFileName):
  url = 'http://central.maven.org/maven2/%s/%s/%s/%s-%s.jar' % (org.replace('.', '/'), name, version, name, version)
  print('Download %s -> %s...' % (url, destFileName))
  urllib.request.urlretrieve(url, destFileName)
  print('  done: %.1f KB' % (os.path.getsize(destFileName)/1024.))

def run(command):
  p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
  out, err = p.communicate()
  if p.returncode != 0:
    print('\nERROR: command "%s" failed:\n%s' % (command, out.decode('utf-8')))
    raise RuntimeError('command "%s" failed' % command)

def anyChanges(srcDir, destJAR):
  if not os.path.exists(destJAR):
    return True

  t1 = os.path.getmtime(destJAR)

  for root, dirNames, fileNames in os.walk(srcDir):
    for fileName in fileNames:
      if os.path.getmtime('%s/%s' % (root, fileName)) > t1:
        return True

  return False

def compileLuceneModules(deps):
  os.chdir('lucene6x/lucene')
  for dep in deps:
    if dep.startswith('analyzers-'):
      # lucene analyzers have two level hierarchy!
      part = dep[10:]
      if anyChanges('analysis/%s' % part, 'build/analysis/%s/lucene-%s-%s.jar' % (part, dep, LUCENE_VERSION)):
        print('build lucene %s JAR...' % dep)
        os.chdir('analysis/%s' % part)
        run('ant jar')
        os.chdir('../..')
    elif anyChanges(dep, 'build/%s/lucene-%s-%s.jar' % (dep, dep, LUCENE_VERSION)):
      print('build lucene %s JAR...' % dep)
      os.chdir(dep)
      run('ant jar')
      os.chdir('..')
  os.chdir('../..')

def compileChangedSources(srcPath, destPath, classPath):
  changedSources = []
  for root, dirNames, fileNames in os.walk(srcPath):
    for fileName in fileNames:
      if fileName.endswith('.java'):
        classFileName = 'build/classes/%s.class' % ('%s/%s' % (root, fileName))[4:-5]
        if not os.path.exists(classFileName) or os.path.getmtime(classFileName) < os.path.getmtime('%s/%s' % (root, fileName)):
          changedSources.append('%s/%s' % (root, fileName))

  if len(changedSources) > 0:
    cmd = ['javac', '-d', destPath]
    cmd.append('-cp')
    cmd.append(':'.join(classPath))
    cmd.extend(changedSources)
    print('compile sources:')
    for fileName in changedSources:
      print('  %s changed' % fileName)
    run(' '.join(cmd))

def getCompileClassPath():
  l = []
  for org, name, version in deps:
    l.append('lib/%s-%s.jar' % (name, version))
  for dep in luceneDeps:
    if dep.startswith('analyzers-'):
      l.append('lucene6x/lucene/build/analysis/%s/lucene-%s-%s.jar' % (dep[10:], dep, LUCENE_VERSION))
      libDir = 'lucene6x/lucene/analysis/%s/lib' % dep[10:]
    else:
      l.append('lucene6x/lucene/build/%s/lucene-%s-%s.jar' % (dep, dep, LUCENE_VERSION))
      libDir = 'lucene6x/lucene/%s/lib' % dep
    if os.path.exists(libDir):
      l.append('%s/*' % libDir)
    
  return l

def getTestClassPath():
  l = getCompileClassPath()
  l.append('build/classes/java')
  for org, name, version in testDeps:
    l.append('lib/%s-%s.jar' % (name, version))
  for dep in luceneTestDeps:
    if dep.startswith('analyzers-'):
      l.append('lucene6x/lucene/build/analysis/%s/lucene-%s-%s.jar' % (dep[10:], dep, LUCENE_VERSION))
    else:
      l.append('lucene6x/lucene/build/%s/lucene-%s-%s.jar' % (dep, dep, LUCENE_VERSION))
  return l

def getArg(option):
  if option in sys.argv:
    i = sys.argv.indexOf(option)
    if i + 2 > len(sys.argv):
      raise RuntimeError('command line option %s requires an argument' % option)
    value = sys.argv[i+1]
    del sys.argv[i:i+2]
    return value
  else:
    return None

def getFlag(option):
  if option in sys.argv:
    sys.argv.remove(option)
    return True
  else:
    return False

def compileSourcesAndDeps():
  if not os.path.exists('lib'):
    print('init: create ./lib directory...')
    os.makedirs('lib')

  if not os.path.exists('build'):
    os.makedirs('build/classes/java')
    os.makedirs('build/classes/test')

  for dep in deps:
    destFileName = 'lib/%s-%s.jar' % (dep[1], dep[2])
    if not os.path.exists(destFileName):
      fetchMavenJAR(*(dep + (destFileName,)))

  if not os.path.exists('lucene6x'):
    print('init: cloning lucene branch_6x to ./lucene6x...')
    run('git clone -b branch_6x https://git-wip-us.apache.org/repos/asf/lucene-solr.git lucene6x')

  compileLuceneModules(luceneDeps)

  # compile luceneserver sources
  jarFileName = 'build/luceneserver-%s.jar' % LUCENE_SERVER_VERSION

  l = getCompileClassPath()
  l.append('build/classes/java')
  compileChangedSources('src/java', 'build/classes/java', l)

  if anyChanges('build/classes/java', jarFileName):
    print('build %s' % jarFileName)
    run('jar cf %s -C build/classes/java .' % jarFileName)

  return jarFileName

def main():
  upto = 1
  while upto < len(sys.argv):
    what = sys.argv[upto]
    upto += 1
    
    if what == 'clean':
      print('cleaning...')
      if os.path.exists('build'):
        shutil.rmtree('build')
    elif what == 'cleanlucene':
      os.chdir('lucene6x')
      run('ant clean')
      os.chdir('..')
    elif what == 'package':
      
      jarFileName = compileSourcesAndDeps()

      destFileName = 'build/luceneserver-%s.zip' % LUCENE_SERVER_VERSION
      rootDirName = 'luceneserver-%s' % LUCENE_SERVER_VERSION

      with zipfile.ZipFile(destFileName, 'w') as z:
        z.write(jarFileName, '%s/lib/luceneserver-%s.jar' % (rootDirName, LUCENE_SERVER_VERSION))
        for org, name, version in deps:
          z.write('lib/%s-%s.jar' % (name, version), '%s/lib/%s-%s.jar' % (rootDirName, name, version))
        for dep in luceneDeps:
          if dep.startswith('analyzers-'):
            z.write('lucene6x/lucene/build/analysis/%s/lucene-%s-%s.jar' % (dep[10:], dep, LUCENE_VERSION), '%s/lib/lucene-%s-%s.jar' % (rootDirName, dep, LUCENE_VERSION))
            libDir = 'lucene6x/lucene/analysis/%s/lib' % dep[10:]
          else:
            z.write('lucene6x/lucene/build/%s/lucene-%s-%s.jar' % (dep, dep, LUCENE_VERSION), '%s/lib/lucene-%s-%s.jar' % (rootDirName, dep, LUCENE_VERSION))
            libDir = 'lucene6x/lucene/%s/lib' % dep
          if os.path.exists(libDir):
            for name in os.listdir(libDir):
              z.write('%s/%s' % (libDir, name), '%s/lib/%s' % (rootDirName, name))

        print('\nWrote %s (%.1f MB)\n' % (destFileName, os.path.getsize(destFileName)/1024./1024.))

    elif what == 'test' or what.startswith('Test'):

      if what.startswith('Test'):
        upto -= 1

      seed = getArg('-seed')
      verbose = getFlag('-verbose')

      jarFileName = compileSourcesAndDeps()

      compileLuceneModules(luceneTestDeps)

      for dep in testDeps:
        destFileName = 'lib/%s-%s.jar' % (dep[1], dep[2])
        if not os.path.exists(destFileName):
          fetchMavenJAR(*(dep + (destFileName,)))

      testCP = getTestClassPath()
      testCP.append('build/classes/test')
      testCP.append(jarFileName)
      compileChangedSources('src/test', 'build/classes/test', testCP)
      for extraFile in ('MockPlugin-hello.txt', 'MockPlugin-lucene-server-plugin.properties'):
        if not os.path.exists('build/classes/test/org/apache/lucene/server/%s' % extraFile):
          shutil.copy('src/test/org/apache/lucene/server/%s' % extraFile,
                      'build/classes/test/org/apache/lucene/server/%s' % extraFile)

      if upto == len(sys.argv):
        # Run all tests
        testSubString = None
        testMethod = None
      elif upto == len(sys.argv)-1:
        testSubString = sys.argv[upto]
        if testSubString != 'package':
          if '.' in testSubString:
            parts = testSubString.split('.')
            if len(parts) != 2:
              raise RuntimeError('test fragment should be either TestFoo or TessFoo.testMethod')
            testSubString = parts[0]
            testMethod = parts[1]
          else:
            testMethod = None
          upto += 1
        else:
          testSubString = None
          testMethod = None
      else:
        raise RuntimeError('at most one test substring can be specified')

      testClasses = []
      for root, dirNames, fileNames in os.walk('build/classes/test'):
        for fileName in fileNames:
          if fileName.startswith('Test') and fileName.endswith('.class') and '$' not in fileName:
            fullPath = '%s/%s' % (root, fileName)
            if testSubString is None or testSubString in fullPath:
              className = fullPath[19:-6].replace('/', '.')
              if testSubString is not None and len(testClasses) == 1:
                raise RuntimeError('test name fragment "%s" is ambiguous, matching at least %s and %s' % (testSubString, testClasses[0], className))
              testClasses.append(className)

      if len(testClasses) == 0:
        if testSubString is None:
          raise RuntimeError('no tests found (wtf?)')
        else:
          raise RuntimeError('no tests match substring "%s"' % testSubString)

      # TODO: also detect if no tests matched the tests.method!

      jvmCount = min(multiprocessing.cpu_count(), len(testClasses))

      if testSubString is not None:
        print('Running test %s' % testClasses[0])
        printOutput = True
      else:
        print('Running %d tests in %d JVMs' % (len(testClasses), jvmCount))
        printOutput = False

      if not os.path.exists('build/test'):
        os.makedirs('build/test')

      t0 = time.time()
      jobs = queue.Queue()

      jvms = []
      for i in range(jvmCount):
        jvm = RunTestsJVM(0, jobs, testCP, verbose, None, printOutput, testMethod=testMethod)
        jvm.start()
        jvms.append(jvm)

      for s in testClasses:
        jobs.put(s)

      for i in range(jvmCount):
        jobs.put(None)

      failCount = 0
      testCount = 0
      suiteCount = 0
      for jvm in jvms:
        jvm.join()
        failCount += jvm.failCount
        testCount += jvm.testCount
        suiteCount += jvm.suiteCount

      totalSec = time.time() - t0
      
      if failCount > 0:
        print('\nFAILURE [%d of %d test cases in %d suites failed in %.1f sec]' % (failCount, testCount, suiteCount, totalSec))
        sys.exit(1)
      elif testCount == 0:
        print('\nFAILURE: no tests ran!')
      else:
        print('\nSUCCESS [%d test cases in %d suites in %.1f sec]' % (testCount, suiteCount, totalSec))
        sys.exit(0)
      
    else:
      raise RuntimeError('unknown target %s' % what)
      
if __name__ == '__main__':
  if len(sys.argv) == 1:
    print('''
    Usage:

      ./build.py clean

        Removes all artifacts.

      ./build.py test

        Runs all tests.

      ./build.py package

        Build install zip to build/luceneserver-VERSION.zip

      ./build.py TestFoo[.testBar]

        Runs a single test class and optionally method.

    You can also combine them, e.g. "clean test package".
     ''')
    sys.exit(1)
    
  main()
