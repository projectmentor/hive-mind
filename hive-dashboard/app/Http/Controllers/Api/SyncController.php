<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

class SyncController extends Controller
{
    private $hivePath;
    
    public function __construct()
    {
        $this->hivePath = env('HIVE_HOME', env('HOME') . '/projects/hive-mind');
    }

    /**
     * Receive entries from remote hive
     */
    public function receive(Request $request): JsonResponse
    {
        $entries = $request->input('entries', []);
        
        try {
            // Process each entry through the CLI to maintain consistency
            foreach ($entries as $entry) {
                if ($entry['op'] === 'remember') {
                    $data = $entry['data'];
                    $tags = implode(',', $data['tags'] ?? []);
                    
                    $cmd = [
                        $this->hivePath . '/hv',
                        'remember',
                        $data['content'],
                        '--tags', $tags,
                        '--importance', (string)($data['importance'] ?? 0.5),
                        '--source', $data['source'] . '_remote'
                    ];
                    
                    $result = shell_exec(escapeshellcmd(implode(' ', array_map('escapeshellarg', $cmd))));
                    Log::info("Synced entry: " . $data['content'], ['result' => $result]);
                }
            }
            
            return response()->json([
                'success' => true,
                'processed' => count($entries)
            ]);
            
        } catch (\Exception $e) {
            Log::error("Sync receive error: " . $e->getMessage());
            return response()->json([
                'success' => false,
                'error' => $e->getMessage()
            ], 500);
        }
    }

    /**
     * Get entries for remote to pull
     */
    public function entries(): JsonResponse
    {
        try {
            $journalFile = $this->hivePath . '/journal/' . date('Y-m-d') . '.jsonl';
            
            $entries = [];
            if (file_exists($journalFile)) {
                $lines = file($journalFile, FILE_IGNORE_NEW_LINES);
                foreach ($lines as $line) {
                    $entry = json_decode($line, true);
                    if ($entry) {
                        $entries[] = $entry;
                    }
                }
            }
            
            return response()->json([
                'success' => true,
                'entries' => $entries
            ]);
            
        } catch (\Exception $e) {
            Log::error("Sync entries error: " . $e->getMessage());
            return response()->json([
                'success' => false,
                'error' => $e->getMessage()
            ], 500);
        }
    }

    /**
     * Get all facts for dashboard (supports ?q= search)
     */
    public function facts(Request $request): JsonResponse
    {
        try {
            $q = $request->query('q', '');
            $safe_q = escapeshellarg($q);
            $cmd = $this->hivePath . '/hv search ' . $safe_q . ' --format json 2>/dev/null';
            $result = shell_exec($cmd);
            $facts = json_decode($result, true) ?: [];

            return response()->json([
                'success' => true,
                'facts' => $facts
            ]);

        } catch (\Exception $e) {
            Log::error("Get facts error: " . $e->getMessage());
            return response()->json([
                'success' => false,
                'error' => $e->getMessage()
            ], 500);
        }
    }
}