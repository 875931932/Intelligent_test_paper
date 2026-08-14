#requires -Version 7.0
$ErrorActionPreference = 'Stop'

$PrototypeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$Port = 18787
$BaseUrl = "http://127.0.0.1:$Port"
$Server = $null
$DeepSeekKeyBeforeTest = $env:DEEPSEEK_API_KEY
$env:DEEPSEEK_API_KEY = ''
$StdOutPath = Join-Path ([System.IO.Path]::GetTempPath()) "exam-paper-prototype-smoke-$PID.out"
$StdErrPath = Join-Path ([System.IO.Path]::GetTempPath()) "exam-paper-prototype-smoke-$PID.err"
$TempFixturePaths = @()

function Invoke-PrototypeJson {
  param(
    [Parameter(Mandatory = $true)][string]$Method,
    [Parameter(Mandatory = $true)][string]$Path,
    [object]$Body
  )
  $Parameters = @{ Method = $Method; Uri = "$BaseUrl$Path"; UseBasicParsing = $true }
  if ($null -ne $Body) {
    $Parameters.ContentType = 'application/json; charset=utf-8'
    $Parameters.Body = ($Body | ConvertTo-Json -Depth 12 -Compress)
  }
  return (Invoke-WebRequest @Parameters).Content | ConvertFrom-Json
}

function Invoke-MultipartUpload {
  param(
    [Parameter(Mandatory = $true)][string]$MaterialArea,
    [Parameter(Mandatory = $true)][string[]]$FilePaths
  )

  $client = [System.Net.Http.HttpClient]::new()
  $form = [System.Net.Http.MultipartFormDataContent]::new()
  try {
    $form.Add([System.Net.Http.StringContent]::new($MaterialArea), 'material_area')
    foreach ($FilePath in $FilePaths) {
      $stream = [System.IO.File]::OpenRead($FilePath)
      $fileContent = [System.Net.Http.StreamContent]::new($stream)
      $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/octet-stream')
      $form.Add($fileContent, 'files', [System.IO.Path]::GetFileName($FilePath))
    }
    $response = $client.PostAsync("$BaseUrl/api/upload", $form).GetAwaiter().GetResult()
    $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    return [pscustomobject]@{
      StatusCode = [int]$response.StatusCode
      Body = $body
      Json = ($body | ConvertFrom-Json)
    }
  } finally {
    $form.Dispose()
    $client.Dispose()
  }
}

try {
  $Server = Start-Process -FilePath $Python -ArgumentList @('server.py') -WorkingDirectory $PrototypeRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $StdOutPath -RedirectStandardError $StdErrPath -Environment @{ PROTOTYPE_PORT = "$Port" }
  $deadline = (Get-Date).AddSeconds(20)
  do {
    try {
      $health = Invoke-PrototypeJson -Method Get -Path '/api/health'
      if ($health.status -eq 'ok') { break }
    } catch {
      Start-Sleep -Milliseconds 300
    }
  } while ((Get-Date) -lt $deadline)
  if ($null -eq $health -or $health.status -ne 'ok') { throw 'Prototype server did not become ready within 20 seconds.' }
  $serverOutput = Get-Content -LiteralPath $StdOutPath, $StdErrPath -Raw -ErrorAction SilentlyContinue
  if ($serverOutput -match 'DeprecationWarning.*cgi|cgi.*deprecated') { throw 'Server emitted a cgi deprecation warning.' }

  $invalidMultipart = Invoke-WebRequest -Method Post -Uri "$BaseUrl/api/upload" -ContentType 'multipart/form-data; boundary=invalid' -Body 'not-a-multipart-body' -UseBasicParsing -SkipHttpErrorCheck
  if ($invalidMultipart.StatusCode -ne 400) { throw 'Invalid multipart assertion failed.' }

  $fixture = Join-Path $PrototypeRoot 'fixtures\course-outline.txt'
  $outlineSupplement = Join-Path ([System.IO.Path]::GetTempPath()) "exam-paper-outline-$PID.txt"
  $teachingOutline = Join-Path ([System.IO.Path]::GetTempPath()) "exam-paper-teaching-outline-$PID.txt"
  $teachingFixture = Join-Path ([System.IO.Path]::GetTempPath()) "exam-paper-teaching-$PID.txt"
  [System.IO.File]::WriteAllText(
    $outlineSupplement,
    "课程考核大纲`n`n重点考查监督微调、LoRA 参数含义和 RAG 评估原则。",
    [System.Text.UTF8Encoding]::new($false)
  )
  [System.IO.File]::WriteAllText(
    $teachingOutline,
    "课程教学大纲`n`n教学目标：理解监督微调、LoRA 参数高效微调和检索增强生成评估的基本原理。",
    [System.Text.UTF8Encoding]::new($false)
  )
  [System.IO.File]::WriteAllText(
    $teachingFixture,
    "第 4 讲 参数高效微调实践`n`nLoRA 的秩 r 影响可训练参数数量和增量表示能力，缩放系数 alpha 用于调整增量更新的尺度；基础模型参数通常冻结，只训练新增的低秩参数。",
    [System.Text.UTF8Encoding]::new($false)
  )
  $TempFixturePaths = @($outlineSupplement, $teachingOutline, $teachingFixture)

  $outlineUpload = Invoke-MultipartUpload -MaterialArea 'outline' -FilePaths @($teachingOutline, $outlineSupplement)
  if ($outlineUpload.StatusCode -ne 200 -or -not $outlineUpload.Json.ok) { throw 'Outline batch upload assertion failed.' }
  $outlineMaterials = @($outlineUpload.Json.state.materials | Where-Object { $_.material_area -eq 'outline' })
  if ($outlineMaterials.Count -ne 2) { throw 'Outline material area assertion failed.' }
  $outlineIds = @($outlineMaterials | ForEach-Object { $_.id })

  $outlineOrganized = Invoke-PrototypeJson -Method Post -Path '/api/organize-outline' -Body @{ material_ids = $outlineIds }
  if (-not $outlineOrganized.ok -or $outlineOrganized.state.framework.framework_run.status -ne 'awaiting_teacher_confirmation' -or $outlineOrganized.state.framework.candidate_anchor_count -lt 2) { throw 'Outline framework organization assertion failed.' }

  $framework = Invoke-PrototypeJson -Method Post -Path '/api/confirm-framework'
  if (-not $framework.ok -or -not $framework.state.framework.confirmed -or $framework.state.framework.framework.anchors.Count -lt 2) { throw 'Framework confirmation assertion failed.' }

  $teachingUpload = Invoke-MultipartUpload -MaterialArea 'teaching_material' -FilePaths @($teachingFixture)
  if ($teachingUpload.StatusCode -ne 200 -or -not $teachingUpload.Json.ok) { throw 'Teaching-material upload assertion failed.' }
  $allMaterials = @($teachingUpload.Json.state.materials)
  $teachingMaterials = @($allMaterials | Where-Object { $_.material_area -eq 'teaching_material' })
  if ($teachingMaterials.Count -ne 1) { throw 'Teaching-material area assertion failed.' }
  $materialId = $teachingMaterials[0].id

  $outlineOrganizeResponse = Invoke-WebRequest -Method Post -Uri "$BaseUrl/api/organize" -ContentType 'application/json; charset=utf-8' -Body ((@{ material_ids = $outlineIds } | ConvertTo-Json -Depth 12 -Compress)) -UseBasicParsing -SkipHttpErrorCheck
  $outlineOrganizeError = $outlineOrganizeResponse.Content | ConvertFrom-Json
  if ($outlineOrganizeResponse.StatusCode -ne 400 -or $outlineOrganizeError.ok -ne $false -or $outlineOrganizeError.error -notmatch '大纲区') { throw 'Outline organize rejection assertion failed.' }

  $organized = Invoke-PrototypeJson -Method Post -Path '/api/organize' -Body @{ material_ids = @($materialId) }
  if (-not $organized.ok -or $organized.state.organization.candidate_run.status -ne 'failed' -or $organized.state.organization.candidate_counts.knowledge_points -ne 0) { throw 'Missing-model configuration assertion failed.' }

  $reset = Invoke-PrototypeJson -Method Post -Path '/api/reset'
  if (-not $reset.ok -or $reset.state.materials.Count -ne 0) { throw 'Reset-state assertion failed.' }
  $cache = Join-Path $PrototypeRoot '.prototype-data\uploads'
  if (Test-Path -LiteralPath $cache) {
    $cachedItems = @(Get-ChildItem -LiteralPath $cache -Force)
    if ($cachedItems.Count -ne 0) { throw 'Reset did not clean the local upload cache.' }
  }
  Write-Output 'SMOKE TEST PASSED'
} finally {
  if ($null -ne $Server -and -not $Server.HasExited) {
    Stop-Process -Id $Server.Id -Force
  }
  $env:DEEPSEEK_API_KEY = $DeepSeekKeyBeforeTest
  foreach ($TempFixturePath in $TempFixturePaths) {
    Remove-Item -LiteralPath $TempFixturePath -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $StdOutPath, $StdErrPath -Force -ErrorAction SilentlyContinue
}
